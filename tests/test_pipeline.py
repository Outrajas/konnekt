import asyncio
import csv
from pathlib import Path

from leadforge import cli
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
    settings = Settings(_env_file=None, output_dir=tmp_path / "output")
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
    assert (settings.output_dir / "drafts" / "acme__chen.md").read_text() == (
        "NEEDS_MANUAL_PERSONALIZATION\n"
    )
