"""Tests for the Wellfound parser."""
from __future__ import annotations

import json

from leadforge.sources.wellfound import (
    build_search_url,
    parse_wellfound,
)


def _wrap(next_data: dict) -> str:
    return (
        "<!DOCTYPE html><html><head></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data)
        + "</script></body></html>"
    )


def _sample_job(**overrides) -> dict:
    base = {
        "id": 1,
        "title": "Senior ML Engineer",
        "slug": "senior-ml-engineer",
        "startup": {
            "name": "Acme AI",
            "website": "https://acme.ai",
            "slug": "acme-ai",
        },
        "location": "Remote",
        "compensation": {"min": "120000", "max": "180000", "currency": "USD"},
        "description": "We use Python and PyTorch.",
    }
    base.update(overrides)
    return base


def test_parses_simple_next_data():
    html = _wrap({"props": {"pageProps": {"jobs": [_sample_job()]}}})
    jobs = parse_wellfound(html)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Acme AI"
    assert job.domain == "acme.ai"
    assert job.title == "Senior ML Engineer"
    assert job.source == "wellfound"
    assert job.location == "Remote"


def test_returns_empty_when_no_next_data_script():
    html = "<html><body>nothing here</body></html>"
    assert parse_wellfound(html) == []


def test_returns_empty_when_json_malformed():
    html = '<html><script id="__NEXT_DATA__">not-json{</script></html>'
    assert parse_wellfound(html) == []


def test_returns_empty_when_jobs_list_missing():
    html = _wrap({"props": {"pageProps": {}}})
    assert parse_wellfound(html) == []


def test_skips_items_without_title():
    item = _sample_job(title="")
    html = _wrap({"props": {"pageProps": {"jobs": [item]}}})
    assert parse_wellfound(html) == []


def test_skips_items_without_company():
    item = _sample_job(startup={})
    html = _wrap({"props": {"pageProps": {"jobs": [item]}}})
    assert parse_wellfound(html) == []


def test_domain_from_website():
    item = _sample_job(startup={"name": "X", "website": "https://www.example-corp.com/about"})
    html = _wrap({"props": {"pageProps": {"jobs": [item]}}})
    job = parse_wellfound(html)[0]
    assert job.domain == "example-corp.com"


def test_domain_falls_back_to_slug():
    item = _sample_job(startup={"name": "NoSite Inc", "slug": "nos-ite"})
    html = _wrap({"props": {"pageProps": {"jobs": [item]}}})
    job = parse_wellfound(html)[0]
    assert job.domain == "nos-ite.wellfound"


def test_salary_prefix_in_description():
    item = _sample_job()
    html = _wrap({"props": {"pageProps": {"jobs": [item]}}})
    job = parse_wellfound(html)[0]
    assert "Salary: USD120000-180000" in job.description
    assert "PyTorch" in job.description


def test_salary_single_value():
    item = _sample_job(compensation={"min": "", "max": "150000", "currency": "USD"})
    html = _wrap({"props": {"pageProps": {"jobs": [item]}}})
    job = parse_wellfound(html)[0]
    assert "Salary: USD150000" in job.description


def test_no_salary_no_prefix():
    item = _sample_job(compensation={})
    html = _wrap({"props": {"pageProps": {"jobs": [item]}}})
    job = parse_wellfound(html)[0]
    assert not job.description.startswith("Salary:")


def test_dedupes_by_url():
    a = _sample_job()
    b = _sample_job()
    html = _wrap({"props": {"pageProps": {"jobs": [a, b]}}})
    jobs = parse_wellfound(html)
    assert len(jobs) == 1


def test_remote_flag_with_no_location():
    item = _sample_job(location="", remote=True)
    html = _wrap({"props": {"pageProps": {"jobs": [item]}}})
    job = parse_wellfound(html)[0]
    assert job.location == "Remote"


def test_alternate_shape_initial_state():
    html = _wrap(
        {"props": {"pageProps": {"initialState": {"jobs": [_sample_job()]}}}}
    )
    jobs = parse_wellfound(html)
    assert len(jobs) == 1


def test_alternate_shape_data_jobs():
    html = _wrap({"props": {"pageProps": {"data": {"jobs": [_sample_job()]}}}})
    jobs = parse_wellfound(html)
    assert len(jobs) == 1


def test_build_search_url_returns_default():
    assert build_search_url() == "https://wellfound.com/jobs"