"""Glassdoor job discovery through the Tier-3 browser."""

from __future__ import annotations

from urllib.parse import quote_plus

from leadforge.models import Job
from leadforge.sources.linkedin import _dedupe
from leadforge.sources.platforms import fetch_platform, parse_platform_page, search_terms
from leadforge.web.fetcher import Fetcher

SEARCH = (
    "https://www.glassdoor.co.in/Job/india-jobs-SRCH_IL.0,5_IN115.htm"
    "?sc.keyword={query}&fromAge=7&sortBy=date_desc"
)


def search_urls(profile, *, limit: int = 2) -> list[str]:
    return [SEARCH.format(query=quote_plus(term)) for term in search_terms(profile, limit)]


def parse_glassdoor(html: str, *, base_url: str = "https://www.glassdoor.co.in") -> list[Job]:
    return parse_platform_page(
        html,
        source="glassdoor",
        base_url=base_url,
        anchor_pattern=r"/job-listing/|/Job/",
    )


async def fetch_glassdoor_jobs(profile, *, fetcher: Fetcher | None = None) -> list[Job]:
    router = fetcher or Fetcher()
    jobs: list[Job] = []
    for url in search_urls(profile):
        try:
            page = await fetch_platform(router, url)
        except Exception:  # noqa: BLE001
            continue
        jobs.extend(parse_glassdoor(page.html, base_url=page.final_url))
    return _dedupe(jobs)
