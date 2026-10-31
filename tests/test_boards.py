"""Tests for the 5 new board parsers (fixture-driven, hermetic)."""
from __future__ import annotations

from pathlib import Path

import pytest

from leadforge.sources.himalayas import parse_himalayas
from leadforge.sources.instahyre import parse_instahyre
from leadforge.sources.remoteok import parse_remoteok
from leadforge.sources.remotive import parse_remotive
from leadforge.sources.unstop import parse_unstop

FIX = Path(__file__).parent / "fixtures" / "sources"


def _load(name: str):
    p = FIX / f"{name}_sample.json"
    if not p.exists():
        pytest.skip(f"fixture missing: {p}")
    return p.read_text(encoding="utf-8")


# ---------------- instahyre ----------------

def test_instahyre_parses_fixture():
    jobs = parse_instahyre(_load("instahyre"))
    assert len(jobs) >= 1
    for job in jobs:
        assert job.source == "instahyre"
        assert job.company
        assert job.title
        assert job.url.startswith("https://www.instahyre.com/job-")
        assert job.domain.endswith(".instahyre")


def test_instahyre_rejects_empty_payload():
    assert parse_instahyre("") == []
    assert parse_instahyre({}) == []


def test_instahyre_dedupes_by_url():
    payload = {"objects": [
        {"title": "T", "public_url": "https://x", "employer": {"company_name": "C"}},
        {"title": "T", "public_url": "https://x", "employer": {"company_name": "C"}},
    ]}
    assert len(parse_instahyre(payload)) == 1


# ---------------- remotive ----------------

def test_remotive_parses_fixture():
    jobs = parse_remotive(_load("remotive"))
    assert len(jobs) >= 1
    for job in jobs:
        assert job.source == "remotive"
        assert job.url.startswith("https://remotive.com/")
        assert job.domain.endswith(".remotive")


def test_remotive_salary_prefix_when_present():
    payload = {"jobs": [{
        "title": "T", "url": "https://r/1",
        "company_name": "C", "salary": "$100k",
    }]}
    jobs = parse_remotive(payload)
    assert "Salary: $100k" in jobs[0].description


# ---------------- remoteok ----------------

def test_remoteok_parses_fixture():
    jobs = parse_remoteok(_load("remoteok"))
    assert len(jobs) >= 1
    for job in jobs:
        assert job.source == "remoteok"
        assert job.url.startswith("https://remoteok.com/")
        assert job.remote is True


def test_remoteok_skips_legal_item():
    payload = [
        {"last_updated": 123, "legal": "..."},
        {"position": "T", "company": "C", "slug": "abc"},
    ]
    jobs = parse_remoteok(payload)
    assert len(jobs) == 1
    assert jobs[0].title == "T"


def test_remoteok_salary_structured():
    payload = [{
        "position": "T", "company": "C", "slug": "abc",
        "salary_min": 100000, "salary_max": 150000,
    }]
    jobs = parse_remoteok(payload)
    assert "Salary: $100000-$150000" in jobs[0].description


# ---------------- himalayas ----------------

def test_himalayas_parses_fixture():
    jobs = parse_himalayas(_load("himalayas"))
    assert len(jobs) >= 1
    for job in jobs:
        assert job.source == "himalayas"
        assert job.domain.endswith(".himalayas")
        assert job.remote is True


def test_himalayas_structured_salary():
    payload = {"jobs": [{
        "title": "T", "companyName": "C", "companySlug": "c",
        "guid": "123", "minSalary": 100, "maxSalary": 200,
        "currency": "USD", "salaryPeriod": "hourly",
    }]}
    jobs = parse_himalayas(payload)
    assert len(jobs) == 1
    assert "Salary: USD100-200 per hourly" in jobs[0].description


# ---------------- unstop ----------------

def test_unstop_parses_fixture():
    jobs = parse_unstop(_load("unstop"))
    assert len(jobs) >= 1
    for job in jobs:
        assert job.source == "unstop"
        assert job.title
        assert job.url.startswith("https://unstop.com/")


def test_unstop_handles_nested_data():
    payload = {"data": {"data": [
        {"title": "T", "public_url": "jobs/t-1234", "organisation": {"name": "C"}},
    ]}}
    jobs = parse_unstop(payload)
    assert len(jobs) == 1
    assert jobs[0].company == "C"


def test_unstop_empty_when_no_data():
    assert parse_unstop({"data": {}}) == []
    assert parse_unstop("") == []