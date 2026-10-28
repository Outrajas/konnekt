"""Tests for email_e2e stage. All offline via injected probe_fn."""
from __future__ import annotations

import pytest

from leadforge import cli as cli_mod
from leadforge.db import connect, init_schema
from leadforge.models import VerificationVerdict
from leadforge.stages.email_e2e import (
    _persist_email,
    find_emails_for_person,
    run_email_e2e,
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    db = tmp_path / "cache" / "leadforge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_mod.settings, "data_dir", tmp_path)
    try:
        monkeypatch.setattr(cli_mod.settings, "db_path", db)
    except (AttributeError, TypeError):
        pass
    conn = connect(db)
    init_schema(conn)
    conn.close()
    return cli_mod.settings


@pytest.fixture
def person_augury(settings):
    conn = connect(settings.db_path)
    try:
        now = "2026-10-28T00:00:00Z"
        conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Augury", "augury.com", "people-e2e", now),
        )
        cid = conn.execute(
            "SELECT id FROM company WHERE domain=?", ("augury.com",)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO person (company_id, first_name, last_name, title, "
            "role_class, status, created_at, updated_at, profile_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, "Jane", "Smith", "Head of Talent", "other", "new", now, now, "ai_ml"),
        )
        pid = conn.execute("SELECT MAX(id) FROM person").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"company_id": cid, "person_id": pid}


def _fetch_person(settings, pid):
    conn = connect(settings.db_path)
    try:
        return conn.execute(
            "SELECT p.*, c.name AS company_name, c.domain AS company_domain "
            "FROM person p JOIN company c ON c.id=p.company_id WHERE p.id=?",
            (pid,),
        ).fetchone()
    finally:
        conn.close()


def test_find_emails_generates_candidates(settings, person_augury):
    row = _fetch_person(settings, person_augury["person_id"])
    candidates = find_emails_for_person(
        row, settings=settings, company="Augury", domain="augury.com",
        smtp_enabled=False,
    )
    assert len(candidates) >= 2
    emails = {c.email for c in candidates}
    assert any("jane" in e for e in emails)
    assert all(e.endswith("@augury.com") for e in emails)


def test_find_emails_smtp_verified(settings, person_augury):
    """Inject async probe_fn + async mx_resolver; verify verdicts become SEND."""
    row = _fetch_person(settings, person_augury["person_id"])

    from leadforge.tools.smtp_verify import Probe

    async def _fake_probe(hosts, address, mail_from, helo, timeout_seconds, ports):
        # Match the real ProbeFn signature and return a Probe instance.
        return Probe(
            code=250,
            message="OK",
            port=25,
            host=hosts[0] if hosts else "mx.augury.com",
            sender_refused=False,
        )

    async def _fake_mx(*args, **kwargs):
        return ["mx.augury.com"]

    candidates = find_emails_for_person(
        row, settings=settings, company="Augury", domain="augury.com",
        smtp_enabled=True, mx_resolver=_fake_mx, probe_fn=_fake_probe,
    )
    sends = [c for c in candidates if c.verification_verdict == VerificationVerdict.SEND]
    assert len(sends) >= 2


def test_find_emails_empty_name_returns_empty(settings):
    conn = connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Empty", "empty.test", "test", "2026-10-28T00:00:00Z"),
        )
        cid = conn.execute("SELECT MAX(id) FROM company").fetchone()[0]
        conn.execute(
            "INSERT INTO person (company_id, first_name, last_name, role_class, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, "", "", "other", "new", "2026-10-28T00:00:00Z", "2026-10-28T00:00:00Z"),
        )
        pid = conn.execute("SELECT MAX(id) FROM person").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    row = _fetch_person(settings, pid)
    candidates = find_emails_for_person(
        row, settings=settings, company="Empty", domain="empty.test", smtp_enabled=False,
    )
    assert candidates == []


def test_persist_email_creates(settings, person_augury):
    conn = connect(settings.db_path)
    try:
        from leadforge.models import Contact, EmailCandidate
        contact = Contact(first_name="Jane", last_name="Smith", domain="augury.com")
        c = EmailCandidate(
            email="jane.smith@augury.com", contact=contact,
            source="pattern", confidence=0.85,
            verification_verdict=VerificationVerdict.SEND,
        )
        created = _persist_email(conn, person_augury["person_id"], c)
        conn.commit()
        assert created is True
        row = conn.execute(
            "SELECT * FROM email_candidate WHERE person_id=?",
            (person_augury["person_id"],),
        ).fetchone()
        assert row["email"] == "jane.smith@augury.com"
        assert row["verification_verdict"] == "send"
    finally:
        conn.close()


def test_persist_email_dedupes(settings, person_augury):
    conn = connect(settings.db_path)
    try:
        from leadforge.models import Contact, EmailCandidate
        contact = Contact(first_name="Jane", last_name="Smith", domain="augury.com")
        c = EmailCandidate(
            email="jane.smith@augury.com", contact=contact,
            source="pattern", confidence=0.85,
        )
        c1 = _persist_email(conn, person_augury["person_id"], c)
        c2 = _persist_email(conn, person_augury["person_id"], c)
        conn.commit()
        assert c1 is True
        assert c2 is False
        count = conn.execute("SELECT COUNT(*) FROM email_candidate").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_run_e2e_skips_wellfound(settings):
    conn = connect(settings.db_path)
    try:
        now = "2026-10-28T00:00:00Z"
        conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Simplismart", "simplismart.wellfound", "wellfound", now),
        )
        cid = conn.execute("SELECT MAX(id) FROM company").fetchone()[0]
        conn.execute(
            "INSERT INTO person (company_id, first_name, last_name, role_class, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, "Bob", "Jones", "other", "new", now, now),
        )
        conn.commit()
    finally:
        conn.close()
    result = run_email_e2e(settings, smtp_enabled=False, dry_run=True)
    assert result.total_candidates == 0


def test_run_e2e_dry_run_does_not_persist(settings, person_augury):
    result = run_email_e2e(settings, smtp_enabled=False, dry_run=True)
    assert result.total_candidates >= 2
    assert result.total_persisted == 0
    conn = connect(settings.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM email_candidate").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_run_e2e_live_persists(settings, person_augury):
    result = run_email_e2e(settings, smtp_enabled=False, dry_run=False)
    assert result.total_candidates >= 2
    assert result.total_persisted >= 2
    conn = connect(settings.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM email_candidate").fetchone()[0]
        assert count >= 2
    finally:
        conn.close()