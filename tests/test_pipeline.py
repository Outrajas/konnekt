import asyncio
import csv
import logging
from pathlib import Path

from leadforge import cli, pipeline
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.llm.client import LLMNotConfigured
from leadforge.models import (
    Contact,
    EmailCandidate,
    EnrichmentResult,
    VerificationMethod,
    VerificationVerdict,
)
from leadforge.pipeline import run_enrichment
from leadforge.stages.replay import ReplayFetcher
from leadforge.stages.walk import WalkResult


def test_pipeline_replay_is_offline_and_returns_provenance(tmp_path) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "pipeline.db")
    contact = Contact(first_name="Jane", last_name="Doe", domain="acme.invalid", company="Acme")
    replay = ReplayFetcher(Path("data/fixtures/sample"))
    verify_calls = []

    async def no_llm(**kwargs):
        raise LLMNotConfigured("fixture mode")

    async def verify(candidates):
        verify_calls.append(candidates)
        return candidates

    results = asyncio.run(
        run_enrichment(
            [contact],
            settings=settings,
            fetch_fn=replay,
            llm_fn=no_llm,
            verify_fn=verify,
            force=True,
        )
    )

    assert len(results) == 1
    assert results[0].status == "found"
    assert results[0].fetches_used == 2
    assert results[0].provenance == [
        "https://acme.invalid/team",
        "https://jane.github.io/",
    ]
    assert results[0].all_candidates[0].email == "jane@acme.invalid"
    assert results[0].all_candidates[0].verification_verdict is VerificationVerdict.UNKNOWN
    assert verify_calls == []


def test_pipeline_skips_persisted_completed_people(tmp_path) -> None:
    db_path = tmp_path / "pipeline.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO company(name,domain,created_at) VALUES(?,?,?)",
        ("Acme", "acme.invalid", "2026-10-15T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO person(company_id,first_name,last_name,status,created_at,updated_at) "
        "VALUES(1,?,?, 'found',?,?)",
        ("Jane", "Doe", "2026-10-15T00:00:00+00:00", "2026-10-15T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    settings = Settings(_env_file=None, db_path=db_path)

    async def unexpected(*args, **kwargs):
        raise AssertionError("completed contact must be skipped")

    results = asyncio.run(
        run_enrichment(
            [Contact(first_name="Jane", last_name="Doe", domain="acme.invalid")],
            settings=settings,
            fetch_fn=unexpected,
            llm_fn=unexpected,
            verify_fn=unexpected,
        )
    )
    assert results == []


def test_enrichment_outputs_review_only_for_sendable_and_manual_draft(
    tmp_path, monkeypatch
) -> None:
    settings = Settings(
        _env_file=None, output_dir=tmp_path / "output", db_path=tmp_path / "pipeline.db"
    )
    contact = Contact(first_name="Riley", last_name="Chen", domain="acme.invalid", company="Acme")
    candidate = EmailCandidate(
        email="riley@acme.invalid",
        contact=contact,
        source="public_web",
        evidence_tier="A",
        found_on_web=True,
        verification_method=VerificationMethod.UNVERIFIED,
        verification_verdict=VerificationVerdict.UNKNOWN,
        confidence=0.8,
    )
    result = EnrichmentResult(
        contact=contact,
        best_email=candidate,
        all_candidates=[candidate],
        status="found",
        stop_reason="email_found",
        fetches_used=1,
        provenance=["https://acme.invalid/team"],
    )

    monkeypatch.setattr(cli, "settings", settings)
    cli._write_enrichment_outputs([result], settings)

    with (settings.output_dir / "review.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 1
    assert rows[0]["approved"] == ""
    assert rows[0]["draft_quality"] == "fail"
    assert "NEEDS_MANUAL_PERSONALIZATION" in rows[0]["draft_issues"]
    assert (settings.output_dir / "drafts" / "acme__chen.md").read_text() == (
        "NEEDS_MANUAL_PERSONALIZATION\n"
    )


def test_suppressed_tier_a_candidate_never_enters_review_csv(tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None, output_dir=tmp_path / "output", db_path=tmp_path / "pipeline.db"
    )
    conn = connect(settings.db_path)
    init_schema(conn)
    company_id = conn.execute(
        "INSERT INTO company(name,domain,created_at) VALUES('Acme','acme.invalid','2026-10-01')"
    ).lastrowid
    conn.execute(
        "INSERT INTO person(company_id,first_name,last_name,created_at,updated_at) "
        "VALUES(?,'Riley','Chen','2026-10-01','2026-10-01')",
        (company_id,),
    )
    conn.execute(
        "INSERT INTO suppression(kind,value,created_at) "
        "VALUES('domain','acme.invalid','2026-10-01')"
    )
    conn.commit()
    conn.close()
    contact = Contact(first_name="Riley", last_name="Chen", domain="acme.invalid", company="Acme")
    candidate = EmailCandidate(
        email="riley@acme.invalid",
        contact=contact,
        source="public_web",
        evidence_tier="A",
        found_on_web=True,
        confidence=0.8,
    )
    result = EnrichmentResult(contact=contact, best_email=candidate, all_candidates=[candidate])
    monkeypatch.setattr(cli, "settings", settings)

    cli._write_enrichment_outputs([result], settings)

    with (settings.output_dir / "review.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    assert rows == []


def test_one_person_failure_does_not_stop_the_next_person(tmp_path, monkeypatch) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "pipeline.db")
    calls = []

    async def fake_walk(*, contact, **kwargs):
        calls.append(contact.first_name)
        if contact.first_name == "Jane":
            raise RuntimeError("page parser failed")
        return WalkResult(
            person_id=0,
            status="found",
            stop_reason="email_found",
            candidates=[],
            pages_visited=[],
            contact=contact,
        )

    monkeypatch.setattr(pipeline, "walk_person", fake_walk)
    contacts = [
        Contact(first_name="Jane", last_name="Doe", domain="example.invalid"),
        Contact(first_name="Riley", last_name="Chen", domain="example.invalid"),
    ]
    results = asyncio.run(
        run_enrichment(
            contacts,
            settings=settings,
            fetch_fn=lambda _url: None,
            llm_fn=lambda **_kwargs: None,
            verify_fn=lambda candidates: candidates,
        )
    )
    assert calls == ["Jane", "Riley"]
    assert [result.status for result in results] == ["not_found", "found"]
    assert results[0].stop_reason == "error: RuntimeError"


def test_info_logs_never_include_email_or_page_text(tmp_path, monkeypatch, caplog) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "pipeline.db")
    sensitive_email = "jane.private@example.invalid"
    sensitive_page_text = "fictional private page excerpt 91c3d"

    async def fake_walk(**_kwargs):
        raise RuntimeError(f"{sensitive_email} {sensitive_page_text}")

    monkeypatch.setattr(pipeline, "walk_person", fake_walk)
    with caplog.at_level(logging.INFO, logger="leadforge.pipeline"):
        asyncio.run(
            run_enrichment(
                [Contact(first_name="Jane", last_name="Doe", domain="example.invalid")],
                settings=settings,
                fetch_fn=lambda _url: None,
                llm_fn=lambda **_kwargs: None,
                verify_fn=lambda candidates: candidates,
            )
        )
    info_text = "\n".join(
        record.getMessage() for record in caplog.records if record.levelno == logging.INFO
    )
    assert sensitive_email not in info_text
    assert sensitive_page_text not in info_text
