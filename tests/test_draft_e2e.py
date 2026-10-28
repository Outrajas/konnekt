"""Tests for draft_e2e stage. All offline via fake LLM."""
from __future__ import annotations

import asyncio

import pytest

from leadforge import cli as cli_mod
from leadforge.db import connect, init_schema
from leadforge.stages.draft_e2e import (
    DraftResponse,
    _verify_evidence,
    _write_draft_file,
    generate_draft_for_person,
    run_draft_e2e,
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    db = tmp_path / "cache" / "leadforge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_mod.settings, "data_dir", tmp_path)
    monkeypatch.setattr(cli_mod.settings, "output_dir", out_dir)
    try:
        monkeypatch.setattr(cli_mod.settings, "db_path", db)
    except (AttributeError, TypeError):
        pass
    conn = connect(db)
    init_schema(conn)
    from leadforge.profiles import ensure_schema as ensure_profiles_schema
    ensure_profiles_schema(conn)
    conn.close()
    return cli_mod.settings


@pytest.fixture
def seeded(settings):
    """Company, job, person, email_candidate + profile."""
    conn = connect(settings.db_path)
    try:
        from leadforge.profiles import ensure_schema as _ensure_ps
        _ensure_ps(conn)
        now = "2026-10-28T00:00:00Z"
        conn.execute(
            "INSERT INTO company(name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Augury", "augury.com", "test", now),
        )
        cid = conn.execute("SELECT MAX(id) FROM company").fetchone()[0]
        conn.execute(
            "INSERT INTO job(company_id, title, url, source, first_seen, last_seen, "
            "location, salary_min, salary_max, salary_currency, tech_stack_json, profile_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, "Senior ML Engineer", "https://augury.com/jobs/1", "test", now, now,
             "Bengaluru", 2000000, 3000000, "INR",
             '["python", "pytorch", "aws"]', "ai_ml"),
        )
        jid = conn.execute("SELECT MAX(id) FROM job").fetchone()[0]
        conn.execute(
            "INSERT INTO person(company_id, first_name, last_name, title, role_class, "
            "status, created_at, updated_at, profile_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, "Jane", "Smith", "Head of Talent", "other", "new", now, now, "ai_ml"),
        )
        pid = conn.execute("SELECT MAX(id) FROM person").fetchone()[0]
        conn.execute(
            "INSERT INTO email_candidate(person_id, email, pattern, source, "
            "evidence_tier, confidence, verification_verdict, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, "jane.smith@augury.com", "first.last", "pattern", "A", 0.85, "send", now),
        )
        # Insert profile row
        conn.execute(
            "INSERT INTO profiles(profile_id, track, skills_json, target_titles_json, "
            "localities_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ai_ml", "ai_ml",
             '["python", "pytorch", "deep learning"]',
             '["machine learning engineer", "ai engineer"]',
             '["India", "remote"]', now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"company_id": cid, "job_id": jid, "person_id": pid}


class _FakeLLM:
    def __init__(self, response: DraftResponse | Exception):
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def chat_json(self, system: str, user: str, *, schema=None, **kwargs):
        self.calls.append((system, user))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _fetch_person_job(settings, pid):
    conn = connect(settings.db_path)
    try:
        return conn.execute(
            "SELECT p.*, c.name AS company_name, j.* FROM person p "
            "JOIN company c ON c.id=p.company_id "
            "LEFT JOIN job j ON j.company_id=c.id WHERE p.id=? LIMIT 1",
            (pid,),
        ).fetchone()
    finally:
        conn.close()


# ---------------- template / dry-run ----------------


def test_template_dry_run(settings, seeded):
    row = _fetch_person_job(settings, seeded["person_id"])
    person = dict(row)
    draft = asyncio.run(generate_draft_for_person(
        person, person, person, {"skills": ["python"], "target_titles": ["ML Engineer"]},
        "Augury", dry_run=True,
    ))
    assert isinstance(draft, DraftResponse)
    assert "Augury" in draft.subject or "role" in draft.subject
    assert "Jane" in draft.body
    assert len(draft.body) >= 100


# ---------------- LLM path ----------------


def test_llm_draft(settings, seeded):
    row = _fetch_person_job(settings, seeded["person_id"])
    person = dict(row)
    fake = _FakeLLM(DraftResponse(
        subject="Quick question about the ML role",
        body="Hi Jane, I saw the ML Engineer opening at Augury and noticed "
             "your work as Head of Talent. " + "x" * 120,
        evidence_used=["recipient_title", "open_role"],
    ))
    draft = asyncio.run(generate_draft_for_person(
        person, person, person, {"skills": ["python"], "target_titles": ["ML Engineer"]},
        "Augury", llm_client=fake,
    ))
    assert "ML role" in draft.subject
    assert len(fake.calls) == 1


def test_llm_not_configured_propagates(settings, seeded):
    from leadforge.llm import LLMNotConfigured
    row = _fetch_person_job(settings, seeded["person_id"])
    person = dict(row)
    fake = _FakeLLM(LLMNotConfigured("no api key"))
    with pytest.raises(LLMNotConfigured):
        asyncio.run(generate_draft_for_person(
            person, person, person, {"skills": [], "target_titles": []},
            "Augury", llm_client=fake,
        ))


# ---------------- attribution ----------------


def test_verify_evidence_no_hallucinations():
    facts = ["recipient_title: Head of Talent", "open_role: ML Engineer"]
    used = ["recipient_title", "open_role"]
    assert _verify_evidence(used, facts) == []


def test_verify_evidence_detects_hallucinations():
    facts = ["recipient_title: Head of Talent"]
    used = ["recipient_title", "hobby: chess"]
    assert _verify_evidence(used, facts) == ["hobby: chess"]


# ---------------- file writing ----------------


def test_write_draft_file(settings):
    path = _write_draft_file(settings, "Augury", "Jane Smith", "Hello", "Body text")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Augury" in content
    assert "Jane Smith" in content
    assert "Hello" in content


# ---------------- orchestrator ----------------


def test_run_e2e_dry_run_no_persist(settings, seeded):
    result = asyncio.run(run_draft_e2e(settings, profile_id="ai_ml", max_people=5, dry_run=True))
    assert result.total_drafted >= 1
    assert result.total_persisted == 0
    conn = connect(settings.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM draft").fetchone()[0] if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='draft'"
        ).fetchone() else 0
        assert count == 0
    finally:
        conn.close()


def test_run_e2e_live_persists(settings, seeded):
    fake = _FakeLLM(DraftResponse(
        subject="Quick question about the ML role at Augury",
        body="Hi Jane, I saw the ML opening at Augury and wanted to reach out. " + "x" * 100,
        evidence_used=["recipient_title", "open_role"],
    ))
    result = asyncio.run(run_draft_e2e(
        settings, profile_id="ai_ml", max_people=5, llm_client=fake,
    ))
    assert result.total_drafted >= 1
    assert result.total_persisted >= 1
    # Verify draft table row
    conn = connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM draft WHERE person_id=?", (seeded["person_id"],)
        ).fetchone()
        assert row is not None
        assert "ML role" in row["subject"]
    finally:
        conn.close()


def test_run_e2e_skips_persons_without_email(settings):
    """Person with no email_candidate should be skipped."""
    conn = connect(settings.db_path)
    try:
        now = "2026-10-28T00:00:00Z"
        conn.execute(
            "INSERT INTO company(name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Orphan", "orphan.test", "test", now),
        )
        cid = conn.execute("SELECT MAX(id) FROM company").fetchone()[0]
        conn.execute(
            "INSERT INTO person(company_id, first_name, last_name, role_class, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, "Bob", "Jones", "other", "new", now, now),
        )
        conn.commit()
    finally:
        conn.close()
    result = asyncio.run(run_draft_e2e(settings, profile_id="ai_ml", max_people=5, dry_run=True))
    # Bob has no email, so not drafted
    names = [p.name for p in result.people]
    assert "Bob Jones" not in names