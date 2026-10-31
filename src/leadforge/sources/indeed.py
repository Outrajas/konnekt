"""Indeed job discovery through the Tier-3 browser."""

from __future__ import annotations

from urllib.parse import quote_plus

from leadforge.models import Job
from leadforge.sources.linkedin import _dedupe
from leadforge.sources.platforms import fetch_platform, parse_platform_page, search_terms
from leadforge.web.fetcher import Fetcher

SEARCH = "https://in.indeed.com/jobs?q={query}&l=India&fromage=7&sort=date"


def search_urls(profile, *, limit: int = 3) -> list[str]:
    return [SEARCH.format(query=quote_plus(term)) for term in search_terms(profile, limit)]


def parse_indeed(html: str, *, base_url: str = "https://in.indeed.com") -> list[Job]:
    return parse_platform_page(
        html,
        source="indeed",
        base_url=base_url,
        anchor_pattern=r"/viewjob\?jk=|/rc/clk",
    )


async def fetch_indeed_jobs(profile, *, fetcher: Fetcher | None = None) -> list[Job]:
    router = fetcher or Fetcher()
    jobs: list[Job] = []
    for url in search_urls(profile):
        try:
            page = await fetch_platform(router, url)
        except Exception:  # noqa: BLE001
            continue
        jobs.extend(parse_indeed(page.html, base_url=page.final_url))
    return _dedupe(jobs)
