"""Tests for job reputation scoring + acceptance gate."""
from __future__ import annotations

import pytest

from leadforge import cli as cli_mod
from leadforge.db import connect
from leadforge.job_persist import persist_jobs
from leadforge.job_reputation import (
    _REPUTATION_THRESHOLD,
    compute_company_reputation,
    is_acceptable,
)
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
        company="Acme",
        domain="acme.test",
        title="Engineer",
        url="https://acme.test/j/1",
        location="Remote",
        employment_type="full-time",
        description="Short desc.",
        source="careers-page",
        match_score=0.5,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _set_invite_score(settings, domain, score):
    conn = connect(settings.db_path)
    try:
        conn.execute("UPDATE company SET invite_email_score=? WHERE domain=?", (score, domain))
        conn.commit()
    finally:
        conn.close()


def test_unknown_company_scores_zero(settings):
    r = compute_company_reputation("nope.test", settings)
    assert r.score == 0.0
    assert r.signals == ["unknown-company"]


def test_single_job_careers_page_short_desc_low_score(settings):
    persist_jobs([_job()], settings)
    r = compute_company_reputation("acme.test", settings)
    assert r.score == 0.10
    assert any("hiring:1" in s for s in r.signals)


def test_active_hiring_bonus(settings):
    persist_jobs(
        [
            _job(url="https://acme.test/j/1", title="A"),
            _job(url="https://acme.test/j/2", title="B"),
            _job(url="https://acme.test/j/3", title="C"),
        ],
        settings,
    )
    r = compute_company_reputation("acme.test", settings)
    assert r.score >= 0.30
    assert any("active-hiring:3" in s for s in r.signals)


def test_known_ats_bonus(settings):
    persist_jobs([_job(source="greenhouse")], settings)
    r = compute_company_reputation("acme.test", settings)
    assert any("known-ats" in s for s in r.signals)
    assert r.score >= 0.20


def test_hn_invite_signal(settings):
    persist_jobs([_job()], settings)
    _set_invite_score(settings, "acme.test", 3)
    r = compute_company_reputation("acme.test", settings)
    assert any("hn-invite" in s for s in r.signals)
    assert r.score >= 0.20


def test_detailed_posting_bonus(settings):
    persist_jobs([_job(description="A" * 600)], settings)
    r = compute_company_reputation("acme.test", settings)
    assert any("detailed-posting" in s for s in r.signals)


def test_score_capped_at_one(settings):
    persist_jobs(
        [
            _job(
                url="https://acme.test/j/1",
                source="greenhouse",
                description="A" * 600,
            ),
            _job(url="https://acme.test/j/2"),
            _job(url="https://acme.test/j/3"),
        ],
        settings,
    )
    _set_invite_score(settings, "acme.test", 1)
    conn = connect(settings.db_path)
    try:
        conn.execute("UPDATE job SET salary_min=100000")
        conn.commit()
    finally:
        conn.close()
    r = compute_company_reputation("acme.test", settings)
    assert r.score == 1.0


def test_acceptable_with_salary(settings):
    persist_jobs([_job()], settings)
    conn = connect(settings.db_path)
    try:
        conn.execute("UPDATE job SET salary_min=100000")
        conn.commit()
        row = conn.execute("SELECT * FROM job LIMIT 1").fetchone()
    finally:
        conn.close()
    r = compute_company_reputation("acme.test", settings)
    ok, reason = is_acceptable(row, r)
    assert ok is True
    assert "salary:" in reason


def test_not_acceptable_without_salary_or_reputation(settings):
    persist_jobs([_job()], settings)
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM job LIMIT 1").fetchone()
    finally:
        conn.close()
    r = compute_company_reputation("acme.test", settings)
    ok, reason = is_acceptable(row, r)
    assert ok is False
    assert "no-salary" in reason


def test_acceptable_via_reputation(settings):
    persist_jobs(
        [
            _job(url="https://acme.test/j/1", source="greenhouse"),
            _job(url="https://acme.test/j/2"),
            _job(url="https://acme.test/j/3"),
        ],
        settings,
    )
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM job LIMIT 1").fetchone()
    finally:
        conn.close()
    r = compute_company_reputation("acme.test", settings)
    assert r.score >= _REPUTATION_THRESHOLD
    ok, reason = is_acceptable(row, r)
    assert ok is True
    assert "reputation:" in reason