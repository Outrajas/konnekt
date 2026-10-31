"""Platform parsers (LinkedIn / Indeed / Glassdoor) and their tier selection."""

from __future__ import annotations

from types import SimpleNamespace

from leadforge.sources.glassdoor import parse_glassdoor
from leadforge.sources.indeed import parse_indeed
from leadforge.sources.linkedin import parse_linkedin, search_urls
from leadforge.sources.platforms import (
    fetch_platform,
    job_postings_from_jsonld,
)

JSONLD = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Machine Learning Engineer",
 "hiringOrganization":{"@type":"Organization","name":"Acme AI"},
 "jobLocation":{"@type":"Place","address":{"addressLocality":"Bengaluru",
   "addressRegion":"Karnataka","addressCountry":"IN"}},
 "employmentType":"FULL_TIME",
 "description":"Build RAG pipelines with PyTorch and Python.",
 "url":"https://www.linkedin.com/jobs/view/1234567890"}
</script></head><body></body></html>
"""

ANCHORS = """
<ul>
  <li><a href="/jobs/view/9876543210">Data Engineer</a><span>Beta Corp</span></li>
  <li><a href="/jobs/view/9876543210">Data Engineer</a><span>Beta Corp</span></li>
  <li><a href="/about">About</a></li>
</ul>
"""


def test_jsonld_jobposting_is_parsed() -> None:
    jobs = job_postings_from_jsonld(JSONLD, "linkedin")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Acme AI"
    assert job.title == "Machine Learning Engineer"
    assert "Bengaluru" in job.location
    assert job.employment_type == "FULL_TIME"
    assert job.source == "linkedin"


def test_linkedin_falls_back_to_anchor_cards_and_dedupes() -> None:
    jobs = parse_linkedin(ANCHORS)
    assert len(jobs) == 1
    assert jobs[0].title == "Data Engineer"
    assert jobs[0].url.endswith("/jobs/view/9876543210")


def test_indeed_and_glassdoor_share_the_platform_parser() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"JobPosting","title":"Backend Engineer",
     "hiringOrganization":{"name":"Gamma Labs"},
     "url":"https://in.indeed.com/viewjob?jk=abc12345",
     "jobLocation":{"address":{"addressLocality":"Pune"}}}
    </script>
    """
    assert parse_indeed(html)[0].company == "Gamma Labs"
    assert parse_glassdoor(html)[0].title == "Backend Engineer"


def test_search_urls_use_profile_titles() -> None:
    profile = SimpleNamespace(target_titles=["ML Engineer", "Data Scientist"])
    urls = search_urls(profile)
    assert len(urls) == 2
    assert "ML+Engineer" in urls[0]
    assert urls[0].startswith("https://www.linkedin.com/jobs/search")


def test_search_urls_fall_back_when_profile_is_empty() -> None:
    assert "software+engineer" in search_urls(SimpleNamespace(target_titles=[]))[0]


class _Result:
    def __init__(self, tier: int) -> None:
        self.tier = tier
        self.html = ""
        self.final_url = "https://example.test/"


class _Fetcher:
    def __init__(self, browser_enabled: bool) -> None:
        self.settings = SimpleNamespace(browser_enabled=browser_enabled)
        self.calls: list[dict] = []

    async def fetch(self, url, **kwargs):
        self.calls.append(kwargs)
        return _Result(kwargs["min_tier"])


async def test_fetch_platform_uses_tier_three_when_browser_enabled() -> None:
    fetcher = _Fetcher(browser_enabled=True)
    result = await fetch_platform(fetcher, "https://www.linkedin.com/jobs/search")
    assert result.tier == 3
    assert fetcher.calls[0]["purpose"] == "job"


async def test_fetch_platform_uses_tier_one_without_browser() -> None:
    fetcher = _Fetcher(browser_enabled=False)
    result = await fetch_platform(fetcher, "https://in.indeed.com/jobs?q=x")
    assert result.tier == 1
    assert len(fetcher.calls) == 1


async def test_fetch_platform_falls_back_to_http_when_browser_tier_fails() -> None:
    from leadforge.web.errors import TierUnavailable

    class _Flaky(_Fetcher):
        async def fetch(self, url, **kwargs):
            self.calls.append(kwargs)
            if kwargs["min_tier"] == 3:
                raise TierUnavailable("no browser")
            return _Result(1)

    fetcher = _Flaky(browser_enabled=True)
    result = await fetch_platform(fetcher, "https://in.indeed.com/jobs?q=x")
    assert result.tier == 1
    assert [call["min_tier"] for call in fetcher.calls] == [3, 1]
