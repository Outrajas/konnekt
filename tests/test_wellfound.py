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
    assert jobs[0].description.startswith("Salary: $140k – $180k.")


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
    assert build_search_url() == (
        "https://wellfound.com/role/l/machine-learning-engineer/india"
    )
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
# ---------------- role-page (JobListingSearchResult) tests ----------------

_ROLE_FIXTURE = Path(__file__).parent / "fixtures" / "sources" / "wellfound_role_sample.json"


def _role_apollo() -> dict:
    if not _ROLE_FIXTURE.exists():
        return {}
    raw = json.loads(_ROLE_FIXTURE.read_text(encoding="utf-8"))
    return raw["props"]["pageProps"]["apolloState"]["data"]


def test_role_page_parses_search_results():
    """Role-page fixture (India ML) parses into Job objects."""
    if not _ROLE_FIXTURE.exists():
        return  # fixture optional
    data = _role_apollo()
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": data}}}})
    jobs = parse_wellfound(html)
    assert len(jobs) >= 1, f"expected >=1 job from role fixture, got {len(jobs)}"
    for job in jobs:
        assert job.source == "wellfound"
        assert job.domain.endswith(".wellfound")
        assert job.url.startswith("https://wellfound.com/jobs/")


def test_role_page_dispatch_uses_search_walker():
    """If a JobListingSearchResult is present, the search walker runs."""
    startup = {
        "id": "10",
        "name": "ZCo",
        "slug": "zco",
        "highlightedJobListings": [{"__ref": "JobListingSearchResult:1"}],
    }
    job = {
        "id": "1", "slug": "engineer", "title": "ML Engineer",
        "compensation": "$100k", "locationNames": ["Bangalore"],
        "remote": False, "jobType": "full-time",
    }
    apollo = {
        "StartupResult:10": startup,
        "JobListingSearchResult:1": job,
    }
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    jobs = parse_wellfound(html)
    assert len(jobs) == 1
    assert jobs[0].company == "ZCo"
    assert jobs[0].domain == "zco.wellfound"
    assert jobs[0].title == "ML Engineer"
    assert jobs[0].location == "Bangalore"


def test_role_page_skips_startup_without_name():
    startup = {
        "id": "10",
        "name": "",
        "slug": "zco",
        "highlightedJobListings": [{"__ref": "JobListingSearchResult:1"}],
    }
    job = {"id": "1", "slug": "e", "title": "Eng"}
    apollo = {"StartupResult:10": startup, "JobListingSearchResult:1": job}
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    assert parse_wellfound(html) == []


def test_role_page_skips_unresolvable_ref():
    startup = {
        "id": "10",
        "name": "ZCo",
        "slug": "zco",
        "highlightedJobListings": [{"__ref": "JobListingSearchResult:MISSING"}],
    }
    apollo = {"StartupResult:10": startup}
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    assert parse_wellfound(html) == []


def test_role_page_remote_flag_preserved():
    startup = {
        "id": "10",
        "name": "ZCo",
        "slug": "zco",
        "highlightedJobListings": [{"__ref": "JobListingSearchResult:1"}],
    }
    job = {"id": "1", "slug": "e", "title": "ML Engineer", "remote": True, "locationNames": []}
    apollo = {"StartupResult:10": startup, "JobListingSearchResult:1": job}
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    jobs = parse_wellfound(html)
    assert len(jobs) == 1
    assert jobs[0].remote is True
    assert jobs[0].location == "Remote"


def test_salary_prefix_strips_equity():
    startup = {
        "id": "10",
        "name": "ZCo",
        "slug": "zco",
        "highlightedJobListings": [{"__ref": "JobListingSearchResult:1"}],
    }
    job = {
        "id": "1",
        "slug": "e",
        "title": "ML",
        "compensation": "$65k – $115k • 0.1% – 0.5%",
    }
    apollo = {"StartupResult:10": startup, "JobListingSearchResult:1": job}
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    jobs = parse_wellfound(html)
    assert jobs[0].description.startswith("Salary: $65k – $115k.")
    assert "0.1%" not in jobs[0].description


# ---------------- build_search_url profile-aware tests ----------------


def test_build_search_url_no_profile_india_default():
    url = build_search_url()
    assert url == "https://wellfound.com/role/l/machine-learning-engineer/india"


def test_build_search_url_no_profile_global():
    url = build_search_url(india=False)
    assert url == "https://wellfound.com/role/r/machine-learning-engineer"


def test_build_search_url_with_profile_india():
    from leadforge.models import OperatorProfile
    profile = OperatorProfile(target_titles=["Data Analyst"])
    url = build_search_url(profile)
    assert url == "https://wellfound.com/role/l/data-analyst/india"


def test_build_search_url_with_profile_global():
    from leadforge.models import OperatorProfile
    profile = OperatorProfile(target_titles=["Software Engineer"])
    url = build_search_url(profile, india=False)
    assert url == "https://wellfound.com/role/r/software-engineer"


def test_build_search_url_with_profile_multiword():
    from leadforge.models import OperatorProfile
    profile = OperatorProfile(target_titles=["Machine Learning Engineer"])
    url = build_search_url(profile)
    assert url == "https://wellfound.com/role/l/machine-learning-engineer/india"
# ---------------- fetch_wellfound_jobs tests ----------------

import asyncio  # noqa: E402

from leadforge.sources.wellfound import fetch_wellfound_jobs  # noqa: E402


class _FakeResponse:
    def __init__(self, html: str, status: int = 200, tier: int = 1):
        self.html = html
        self.status_code = status
        self.tier = tier
        self.final_url = ""


class _FakeFetcher:
    def __init__(self, html: str, fail: bool = False):
        self.html = html
        self.fail = fail
        self.calls: list[dict] = []

    async def fetch(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.fail:
            from leadforge.web.errors import FetchError

            raise FetchError("simulated")
        return _FakeResponse(self.html)


def _search_apollo_for_test():
    startup = {
        "id": "10",
        "name": "ZCo",
        "slug": "zco",
        "highlightedJobListings": [{"__ref": "JobListingSearchResult:1"}],
    }
    job = {
        "id": "1",
        "slug": "senior-ml",
        "title": "Senior ML Engineer",
        "compensation": "$140k",
        "locationNames": ["Bangalore"],
        "remote": False,
    }
    return {
        "StartupResult:10": startup,
        "JobListingSearchResult:1": job,
    }


def test_fetch_wellfound_jobs_returns_parsed_and_filtered():
    apollo = _search_apollo_for_test()
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    fake = _FakeFetcher(html)
    jobs = asyncio.run(fetch_wellfound_jobs(fetcher=fake))
    assert len(jobs) == 1
    assert jobs[0].title == "Senior ML Engineer"
    assert jobs[0].company == "ZCo"


def test_fetch_wellfound_jobs_uses_force_httpx():
    apollo = _search_apollo_for_test()
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    fake = _FakeFetcher(html)
    asyncio.run(fetch_wellfound_jobs(fetcher=fake))
    assert fake.calls, "fetcher was not called"
    call = fake.calls[0]
    assert call.get("force_httpx") is True
    assert call.get("min_tier") == 1
    assert call.get("max_tier") == 1
    assert call.get("purpose") == "job"


def test_fetch_wellfound_jobs_returns_empty_on_fetch_error():
    fake = _FakeFetcher("", fail=True)
    jobs = asyncio.run(fetch_wellfound_jobs(fetcher=fake))
    assert jobs == []


def test_fetch_wellfound_jobs_returns_empty_on_empty_html():
    fake = _FakeFetcher("")
    jobs = asyncio.run(fetch_wellfound_jobs(fetcher=fake))
    assert jobs == []


def test_fetch_wellfound_jobs_uses_profile_url():
    from leadforge.models import OperatorProfile

    profile = OperatorProfile(target_titles=["Data Analyst"])
    apollo = _search_apollo_for_test()
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    fake = _FakeFetcher(html)
    asyncio.run(fetch_wellfound_jobs(profile=profile, fetcher=fake))
    url = fake.calls[0]["url"]
    assert url == "https://wellfound.com/role/l/data-analyst/india"


def test_fetch_wellfound_jobs_global_url():
    apollo = _search_apollo_for_test()
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    fake = _FakeFetcher(html)
    asyncio.run(fetch_wellfound_jobs(fetcher=fake, india=False))
    url = fake.calls[0]["url"]
    assert url == "https://wellfound.com/role/r/machine-learning-engineer"


def test_fetch_wellfound_jobs_entry_level_strict():
    """Strict mode drops India jobs only if not entry-level - verify lenient still accepts."""
    apollo = _search_apollo_for_test()
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    fake = _FakeFetcher(html)
    # Strict would drop "Senior ML Engineer" if not India... but loc=India, so strict also keeps it
    jobs = asyncio.run(fetch_wellfound_jobs(fetcher=fake, entry_level_mode="strict"))
    assert len(jobs) == 1  # India keeps it regardless of mode


# ---------------- salary prefix for equity-only ----------------

def test_salary_prefix_no_equity_suppressed():
    startup = {
        "id": "10",
        "name": "ZCo",
        "slug": "zco",
        "highlightedJobListings": [{"__ref": "JobListingSearchResult:1"}],
    }
    job = {
        "id": "1",
        "slug": "e",
        "title": "ML",
        "compensation": "No equity",
    }
    apollo = {"StartupResult:10": startup, "JobListingSearchResult:1": job}
    html = _wrap({"props": {"pageProps": {"apolloState": {"data": apollo}}}})
    jobs = parse_wellfound(html)
    assert not jobs[0].description.startswith("Salary:")