"""Hirist job source (India tech jobs)."""
from __future__ import annotations

import json

from leadforge.models import Job
from leadforge.seniority import infer_seniority


def parse_hirist(raw: str | dict, *, profile=None) -> list[Job]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        data = raw

    items = data.get("jobs", []) if isinstance(data, dict) else []
    jobs: list[Job] = []
    for item in items:
        title = (item.get("title") or "").strip()
        company = (item.get("company") or item.get("companyName") or "").strip()
        url = (item.get("url") or item.get("jobUrl") or "").strip()
        if not (title and company and url):
            continue

        desc = item.get("description") or ""
        loc = item.get("location") or item.get("locations") or "India"
        if isinstance(loc, list):
            loc = ", ".join(str(x) for x in loc)

        slug = "".join(c if c.isalnum() else "-" for c in company.lower()).strip("-")
        domain = f"{slug}.hirist" if slug else "unknown.hirist"

        jobs.append(Job(
            company=company,
            domain=domain,
            title=title,
            url=url,
            location=str(loc),
            description=str(desc)[:8000],
            source="hirist",
            seniority=infer_seniority(title, str(desc)),
        ))
    return jobs


async def fetch_hirist_jobs(profile, *, fetcher=None) -> list[Job]:
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher
    router = fetcher or Fetcher(settings)
    try:
        resp = await router.fetch(
            "https://www.hirist.tech/jobs",  # placeholder if they have a JSON endpoint
            min_tier=1, max_tier=1, purpose="job",
        )
    except FetchError:
        return []
    # If HTML, we need a parser; fallback to empty
    return parse_hirist(resp.html or "", profile=profile)