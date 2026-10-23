"""Tests for the Wellfound parser (Apollo-cache shape)."""
from __future__ import annotations

import json
from pathlib import Path

from leadforge.sources.wellfound import (
    build_search_url,
    filter_jobs_for_operator,
    parse_wellfound,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "sources" / "wellfound_sample.json"


def _wrap(next_data: dict | str) -> str:
    body = next_data if isinstance(next_data, str) else json.dumps(next_data)
    return (
        "<!DOCTYPE html><html><head></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + body
        + "</script></body></html>"
    )


def _apollo(jobs: list[dict], startups: list[dict]) -> dict:
    data: dict = {"ROOT_QUERY": {"__typename": "Query"}}
    for s in startups:
        data[f"Startup:{s['id']}"] = {"__typename": "Startup", **s}
    for j in jobs:
        data[f"JobListing:{j['id']}"] = {"__typename": "JobListing", **j}
    return {"props": {"pageProps": {"apolloState": {"data": data}}}}


def _sample_job(**overrides) -> dict:
    base = {
        "id": "9000001",
        "slug": "senior-ml-engineer",
        "title": "Senior ML Engineer",
        "compensation": "$140k – $180k",
        "locationNames": ["San Francisco"],
        "remote": True,
        "startup": {"__ref": "Startup:1"},
    }
    base.update(overrides)
    return base


def _sample_startup(**overrides) -> dict:
    base = {"id": "1", "name": "Acme AI", "slug": "acme-ai"}
    base.update(overrides)
    return base


def test_parses_simple_apollo_response():
    html = _wrap(_apollo([_sample_job()], [_sample_startup()]))
    jobs = parse_wellfound(html)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Acme AI"
    assert job.domain == "acme-ai.wellfound"
    assert job.title == "Senior ML Engineer"
    assert job.url == "https://wellfound.com/jobs/9000001-senior-ml-engineer"
    assert job.source == "wellfound"


def test_returns_empty_when_no_next_data():
    assert parse_wellfound("<html><body>nope</body></html>") == []


def test_returns_empty_when_json_malformed():
    html = '<html><script id="__NEXT_DATA__">not-json{</script></html>'
    assert parse_wellfound(html) == []


def test_returns_empty_when_apollo_missing():
    html = _wrap({"props": {"pageProps": {}}})
    assert parse_wellfound(html) == []


def test_skips_item_without_title():
    html = _wrap(_apollo([_sample_job(title="")], [_sample_startup()]))
    assert parse_wellfound(html) == []


def test_skips_item_without_startup_ref():
    html = _wrap(_apollo([_sample_job(startup=None)], [_sample_startup()]))
    assert parse_wellfound(html) == []


def test_skips_item_when_startup_missing_from_apollo():
    html = _wrap(_apollo([_sample_job()], []))  # startup not in cache
    assert parse_wellfound(html) == []


def test_skips_item_when_startup_missing_name():
    html = _wrap(_apollo([_sample_job()], [_sample_startup(name="")]))
    assert parse_wellfound(html) == []


def test_skips_item_when_startup_missing_slug():
    html = _wrap(_apollo([_sample_job()], [_sample_startup(slug="")]))
    assert parse_wellfound(html) == []


def test_domain_is_slug_wellfound():
    html = _wrap(_apollo([_sample_job()], [_sample_startup(slug="example-org")]))
    jobs = parse_wellfound(html)
    assert jobs[0].domain == "example-org.wellfound"


def test_url_uses_id_and_slug():
    html = _wrap(_apollo([_sample_job(id="123", slug="great-role")], [_sample_startup()]))
    jobs = parse_wellfound(html)
    assert jobs[0].url == "https://wellfound.com/jobs/123-great-role"


def test_salary_prefix_added_when_compensation_present():
    html = _wrap(_apollo([_sample_job(compensation="$140k – $180k")], [_sample_startup()]))
    jobs = parse_wellfound(html)
    assert jobs[0].description.startswith("Salary: $140k – $180k")


def test_salary_prefix_skipped_when_compensation_empty():
    html = _wrap(_apollo([_sample_job(compensation="")], [_sample_startup()]))
    jobs = parse_wellfound(html)
    assert not jobs[0].description.startswith("Salary:")


def test_salary_prefix_skipped_when_compensation_undisclosed():
    html = _wrap(_apollo([_sample_job(compensation="Undisclosed")], [_sample_startup()]))
    jobs = parse_wellfound(html)
    assert not jobs[0].description.startswith("Salary:")


def test_location_from_location_names():
    html = _wrap(_apollo([_sample_job(locationNames=["New York"])], [_sample_startup()]))
    jobs = parse_wellfound(html)
    assert jobs[0].location == "New York"


def test_location_remote_fallback():
    html = _wrap(_apollo([_sample_job(locationNames=[], remote=True)], [_sample_startup()]))
    jobs = parse_wellfound(html)
    assert jobs[0].location == "Remote"


def test_location_none_when_neither():
    html = _wrap(_apollo([_sample_job(locationNames=[], remote=False)], [_sample_startup()]))
    jobs = parse_wellfound(html)
    assert jobs[0].location is None


def test_dedupes_by_url():
    jobs_list = [_sample_job(), _sample_job()]
    html = _wrap(_apollo(jobs_list, [_sample_startup()]))
    # The dict construction will overwrite the duplicate key, so only 1 job
    assert len(parse_wellfound(html)) == 1


def test_ignores_non_joblisting_keys():
    html = _wrap(_apollo([_sample_job()], [_sample_startup()]))
    jobs = parse_wellfound(html)
    # Only one Job returned even though apollo has Startup and ROOT_QUERY too
    assert len(jobs) == 1


def test_skips_remote_config_keys():
    """A JobListingRemoteConfig entry must not be treated as a job."""
    apollo = _apollo([_sample_job()], [_sample_startup()])
    apollo["props"]["pageProps"]["apolloState"]["data"][
        "JobListingRemoteConfig:99"
    ] = {"__typename": "JobListingRemoteConfig", "id": "99", "kind": "REMOTE"}
    html = _wrap(apollo)
    jobs = parse_wellfound(html)
    assert len(jobs) == 1


def test_parses_real_fixture():
    """Golden test against a real Wellfound response (pruned to 5 jobs)."""
    if not _FIXTURE.exists():
        return  # fixture is optional; skip in stripped environments
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    html = _wrap(raw)
    jobs = parse_wellfound(html)
    assert len(jobs) >= 3, f"expected at least 3 jobs from real fixture, got {len(jobs)}"
    for job in jobs:
        assert job.source == "wellfound"
        assert job.company
        assert job.domain.endswith(".wellfound")
        assert job.title
        assert job.url.startswith("https://wellfound.com/jobs/")


def test_build_search_url_default():
    assert build_search_url() == "https://wellfound.com/jobs"
# ---------------- filter_jobs_for_operator tests ----------------


def _make_job(**overrides):
    from leadforge.models import Job
    base = dict(
        company="X",
        domain="x.wellfound",
        title="Engineer",
        url="https://wellfound.com/jobs/1-x",
        location="",
        source="wellfound",
        remote=False,
    )
    base.update(overrides)
    return Job(**base)


def test_filter_accepts_india_jobs():
    jobs = [
        _make_job(location="Bangalore"),
        _make_job(location="Mumbai, India"),
        _make_job(location="New Delhi"),
    ]
    assert len(filter_jobs_for_operator(jobs)) == 3


def test_filter_rejects_non_india_non_remote():
    jobs = [_make_job(location="New York", remote=False)]
    assert filter_jobs_for_operator(jobs) == []


def test_filter_rejects_non_india_remote_senior():
    jobs = [_make_job(location="New York", title="Senior ML Engineer", remote=True)]
    assert filter_jobs_for_operator(jobs) == []


def test_filter_accepts_remote_entry_level():
    jobs = [_make_job(location="New York", title="Junior ML Engineer", remote=True)]
    assert len(filter_jobs_for_operator(jobs)) == 1


def test_filter_rejects_remote_bare_title_strict():
    jobs = [_make_job(location="New York", title="Engineer", remote=True)]
    assert filter_jobs_for_operator(jobs) == []


def test_filter_lenient_accepts_bare_title():
    jobs = [_make_job(location="New York", title="Engineer", remote=True)]
    assert len(filter_jobs_for_operator(jobs, entry_level_mode="lenient")) == 1


def test_filter_rejects_seniority_even_lenient():
    for senior_title in [
        "Senior Engineer",
        "Staff Engineer",
        "Principal Architect",
        "Team Lead",
        "Head of Engineering",
        "VP of Engineering",
        "Director of Engineering",
        "Engineering Manager",
    ]:
        jobs = [_make_job(location="New York", title=senior_title, remote=True)]
        assert filter_jobs_for_operator(
            jobs, entry_level_mode="lenient"
        ) == [], f"should reject {senior_title!r}"


def test_filter_accepts_india_regardless_of_title():
    jobs = [_make_job(location="Bangalore", title="Senior Engineer")]
    assert len(filter_jobs_for_operator(jobs)) == 1


def test_filter_empty_location_rejected():
    jobs = [_make_job(location="", remote=False)]
    assert filter_jobs_for_operator(jobs) == []


def test_filter_empty_location_remote_entry_accepted():
    jobs = [_make_job(location="", remote=True, title="Junior Engineer")]
    assert len(filter_jobs_for_operator(jobs)) == 1


def test_filter_disable_india_always_ok():
    jobs = [_make_job(location="Bangalore")]
    assert filter_jobs_for_operator(jobs, india_always_ok=False) == []


def test_filter_disable_remote_entry():
    jobs = [_make_job(location="New York", remote=True, title="Junior Engineer")]
    assert filter_jobs_for_operator(jobs, allow_remote_entry_level=False) == []


def test_filter_real_fixture_subset_of_parsed():
    if not _FIXTURE.exists():
        return
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    jobs = parse_wellfound(_wrap(raw))
    filtered = filter_jobs_for_operator(jobs)
    assert len(filtered) <= len(jobs)