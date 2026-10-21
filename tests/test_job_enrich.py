"""Tests for job enrichment (salary + tech)."""
from __future__ import annotations

import json

import pytest

from leadforge import cli as cli_mod
from leadforge.db import connect
from leadforge.job_enrich import enrich_job_row
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
        company="Acme",
        domain="acme.test",
        title="ML Engineer",
        url="https://acme.test/j/1",
        location="Remote",
        employment_type="full-time",
        description="",
        source="greenhouse",
        match_score=0.7,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _persist_one(settings, description: str) -> int:
    url_to_id, _ = persist_jobs([_job(description=description)], settings)
    return url_to_id["https://acme.test/j/1"]


def test_enrich_extracts_salary_from_description(settings):
    jid = _persist_one(settings, "Salary: $120k-$180k annually. Python, AWS.")
    result = enrich_job_row(jid, settings)
    assert result is not None
    assert result.salary is not None
    assert result.salary.min_amount == 120_000
    assert result.salary.max_amount == 180_000
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM job WHERE id=?", (jid,)).fetchone()
        assert row["salary_min"] == 120_000
        assert row["salary_max"] == 180_000
        assert row["salary_currency"] == "USD"
        assert row["salary_source"] == "regex"
        assert row["enriched_at"] is not None
    finally:
        conn.close()


def test_enrich_extracts_tech_stack(settings):
    jid = _persist_one(settings, "We use Python, PyTorch, and AWS.")
    result = enrich_job_row(jid, settings)
    assert result is not None
    assert "python" in result.tech_stack
    assert "pytorch" in result.tech_stack
    assert "aws" in result.tech_stack
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM job WHERE id=?", (jid,)).fetchone()
        stored = json.loads(row["tech_stack_json"])
        assert "python" in stored
    finally:
        conn.close()


def test_enrich_skips_already_enriched(settings):
    jid = _persist_one(settings, "Salary: $120k. Python.")
    first = enrich_job_row(jid, settings)
    assert first is not None
    assert first.skipped is False

    second = enrich_job_row(jid, settings)
    assert second is not None
    assert second.skipped is True
    assert second.salary is not None


def test_enrich_returns_none_for_missing_id(settings):
    assert enrich_job_row(99999, settings) is None


def test_enrich_no_salary_sets_source_none(settings):
    jid = _persist_one(settings, "Great culture, competitive pay.")
    result = enrich_job_row(jid, settings)
    assert result is not None
    assert result.salary is None
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM job WHERE id=?", (jid,)).fetchone()
        assert row["salary_source"] == "none"
        assert row["enriched_at"] is not None
    finally:
        conn.close()


def test_enrich_picks_salary_from_title_if_desc_empty(settings):
    url_to_id, _ = persist_jobs(
        [_job(title="Engineer - $100k-$150k", description="")], settings
    )
    jid = url_to_id["https://acme.test/j/1"]
    result = enrich_job_row(jid, settings)
    assert result is not None
    assert result.salary is not None
    assert result.salary.min_amount == 100_000


def test_enrich_word_boundary_skills(settings):
    jid = _persist_one(settings, "3 years of experience required with algorithms.")
    result = enrich_job_row(jid, settings)
    assert result is not None
    assert "r" not in result.tech_stack
    assert "go" not in result.tech_stack