"""LinkedIn job discovery through the Tier-3 browser.

LinkedIn is the one platform in this project that needs a real browser: the
guest job search renders client-side, and the personalised surfaces sit behind
the operator's own signed-in profile. Nothing here logs in programmatically â€”
the persistent browser profile is signed in once, by hand.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from leadforge.models import Job
from leadforge.sources.platforms import fetch_platform, parse_platform_page, search_terms
from leadforge.web.fetcher import Fetcher

SEARCH = (
    "https://www.linkedin.com/jobs/search"
    "?keywords={query}&location=India&f_TPR=r604800&sortBy=DD&f_AL=true"
)


def search_urls(profile, *, limit: int = 3) -> list[str]:
    return [SEARCH.format(query=quote_plus(term)) for term in search_terms(profile, limit)]


def parse_linkedin(html: str, *, base_url: str = "https://www.linkedin.com") -> list[Job]:
    return parse_platform_page(
        html,
        source="linkedin",
        base_url=base_url,
        anchor_pattern=r"/jobs/view/",
    )


async def fetch_linkedin_jobs(profile, *, fetcher: Fetcher | None = None) -> list[Job]:
    router = fetcher or Fetcher()
    jobs: list[Job] = []
    for url in search_urls(profile):
        try:
            page = await fetch_platform(router, url)
        except Exception:  # noqa: BLE001 - a blocked platform must not kill the run
            continue
        jobs.extend(parse_linkedin(page.html, base_url=page.final_url))
    return _dedupe(jobs)


def _dedupe(jobs: list[Job]) -> list[Job]:
    seen: set[str] = set()
    unique: list[Job] = []
    for job in jobs:
        if job.url in seen:
            continue
        seen.add(job.url)
        unique.append(job)
    return unique
