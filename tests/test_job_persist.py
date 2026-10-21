"""Tests for job persistence."""
from __future__ import annotations

import pytest

from leadforge import cli as cli_mod
from leadforge.db import connect
from leadforge.job_persist import persist_jobs
from leadforge.models import Job


@pytest.fixture
def settings(tmp_path, monkeypatch):
    db = tmp_path / "cache" / "leadforge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_mod.settings, "data_dir", tmp_path)
    try:
        monkeypatch.setattr(cli_mod.settings, "db_path", db)
    except (AttributeError, TypeError):
        pass
    return cli_mod.settings


def _job(**kwargs) -> Job:
    defaults = dict(
        company="Acme Corp",
        domain="acme.test",
        title="Senior Python Engineer",
        url="https://acme.test/jobs/1",
        location="Remote",
        employment_type="full-time",
        description="We use Python, SQL, AWS. $120k-$180k per year.",
        source="greenhouse",
        match_score=0.7,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def test_persist_creates_company_and_job(settings):
    url_to_id, stats = persist_jobs([_job()], settings)
    assert stats.companies_created == 1
    assert stats.jobs_created == 1
    assert stats.jobs_updated == 0
    assert len(url_to_id) == 1
    job_id = url_to_id["https://acme.test/jobs/1"]
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
        assert row is not None
        assert row["title"] == "Senior Python Engineer"
        assert row["description"].startswith("We use Python")
        company = conn.execute("SELECT * FROM company WHERE domain='acme.test'").fetchone()
        assert company is not None
        assert company["name"] == "Acme Corp"
    finally:
        conn.close()


def test_persist_is_idempotent_per_url(settings):
    persist_jobs([_job()], settings)
    _, stats = persist_jobs([_job(match_score=0.9)], settings)
    assert stats.jobs_created == 0
    assert stats.jobs_updated == 1
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM job WHERE url='https://acme.test/jobs/1'").fetchone()
        assert row["match_score"] == 0.9
    finally:
        conn.close()


def test_persist_dedups_companies_across_jobs(settings):
    jobs = [
        _job(url="https://acme.test/jobs/1", title="Backend"),
        _job(url="https://acme.test/jobs/2", title="Frontend"),
        _job(url="https://acme.test/jobs/3", title="DevOps"),
    ]
    _, stats = persist_jobs(jobs, settings)
    assert stats.companies_created == 1
    assert stats.jobs_created == 3
    conn = connect(settings.db_path)
    try:
        companies = conn.execute("SELECT COUNT(*) FROM company").fetchone()[0]
        assert companies == 1
    finally:
        conn.close()


def test_persist_skips_jobs_without_domain(settings):
    _, stats = persist_jobs([_job(domain="")], settings)
    assert stats.jobs_created == 0


def test_persist_accepts_empty_list(settings):
    url_to_id, stats = persist_jobs([], settings)
    assert url_to_id == {}
    assert stats.jobs_created == 0


def test_persist_handles_multiple_companies(settings):
    jobs = [
        _job(domain="a.test", url="https://a.test/1"),
        _job(domain="b.test", url="https://b.test/1", company="B Co"),
        _job(domain="a.test", url="https://a.test/2"),
    ]
    _, stats = persist_jobs(jobs, settings)
    assert stats.companies_created == 2
    assert stats.jobs_created == 3
    # companies_seen only increments for repeat domains within a single call;
    # cross-call "seen" is not tracked (that's fine - the DB upsert handles it).