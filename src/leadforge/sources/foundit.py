"""Foundit job source."""
from __future__ import annotations

import json

from leadforge.models import Job
from leadforge.seniority import infer_seniority


def parse_foundit(raw: str | dict, *, profile=None) -> list[Job]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        data = raw

    items = data.get("jobList", []) if isinstance(data, dict) else []
    jobs: list[Job] = []
    for item in items:
        title = (item.get("jobTitle") or item.get("title") or "").strip()
        company = (item.get("companyName") or "").strip()
        url = (item.get("jobUrl") or item.get("url") or "").strip()
        if not (title and company and url):
            continue

        desc = item.get("description") or item.get("jobDesc") or ""
        loc = item.get("location") or "India"
        slug = "".join(c if c.isalnum() else "-" for c in company.lower()).strip("-")
        domain = f"{slug}.foundit" if slug else "unknown.foundit"

        jobs.append(Job(
            company=company,
            domain=domain,
            title=title,
            url=url,
            location=str(loc),
            description=str(desc)[:8000],
            source="foundit",
            seniority=infer_seniority(title, str(desc)),
        ))
    return jobs


async def fetch_foundit_jobs(profile, *, fetcher=None) -> list[Job]:
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher
    router = fetcher or Fetcher(settings)
    try:
        resp = await router.fetch(
            "https://www.foundit.in/api/v1/search",  # placeholder
            min_tier=1, max_tier=1, purpose="job",
        )
    except FetchError:
        return []
    return parse_foundit(resp.html or "", profile=profile)