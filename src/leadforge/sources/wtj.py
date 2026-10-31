"""Welcome to the Jungle source (Next.js Apollo State)."""
from __future__ import annotations

import json
import re

from leadforge.models import Job
from leadforge.seniority import infer_seniority

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def parse_wtj(raw: str | dict, *, profile=None) -> list[Job]:
    if not raw:
        return []
    match = _NEXT_DATA_RE.search(raw)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    # Try to find the jobs list in the deep Apollo state
    props = data.get("props", {}).get("pageProps", {})
    apollo = props.get("apolloState", {}).get("data", {})
    
    jobs: list[Job] = []
    for key, val in apollo.items():
        if not key.startswith("Job:"):
            continue
        title = val.get("title") or ""
        company = val.get("company", {}).get("name") or "WTJ Company"
        company_slug = val.get("company", {}).get("slug")
        job_slug = val.get("slug")
        url = (
            "https://www.welcometothejungle.com/en/companies/"
            f"{company_slug}/jobs/{job_slug}"
        )
        desc = val.get("description") or ""
        
        slug = "".join(c if c.isalnum() else "-" for c in company.lower()).strip("-")
        jobs.append(Job(
            company=company,
            domain=f"{slug}.wtj" if slug else "unknown.wtj",
            title=title,
            url=url,
            location=val.get("location") or "Various",
            description=desc[:8000],
            source="wtj",
            remote=True,
            seniority=infer_seniority(title, desc),
        ))
    return jobs


async def fetch_wtj_jobs(profile, *, fetcher=None) -> list[Job]:
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher
    router = fetcher or Fetcher(settings)
    try:
        resp = await router.fetch(
            "https://www.welcometothejungle.com/en/jobs?query=python",
            min_tier=1, max_tier=1, purpose="job",
        )
    except FetchError:
        return []
    return parse_wtj(resp.html or "", profile=profile)