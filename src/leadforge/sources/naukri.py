"""Naukri job source (requires session warming for Akamai)."""
from __future__ import annotations

import json

from leadforge.models import Job
from leadforge.seniority import infer_seniority


def parse_naukri(raw: str | dict, *, profile=None) -> list[Job]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        data = raw

    # Naukri V2 API structure
    items = data.get("noOfResults", []) if isinstance(data, dict) else []
    jobs: list[Job] = []
    for item in items:
        title = (item.get("title") or "").strip()
        company = (item.get("companyName") or "").strip()
        url = (item.get("jdURL") or "").strip()
        if not (title and company and url):
            continue

        desc = item.get("jobDescription") or ""
        loc = item.get("placeholders", [{}])[0].get("label") or "India"
        slug = "".join(c if c.isalnum() else "-" for c in company.lower()).strip("-")
        domain = f"{slug}.naukri" if slug else "unknown.naukri"

        jobs.append(Job(
            company=company,
            domain=domain,
            title=title,
            url=url,
            location=str(loc),
            description=str(desc)[:8000],
            source="naukri",
            seniority=infer_seniority(title, str(desc)),
        ))
    return jobs


async def fetch_naukri_jobs(profile, *, fetcher=None) -> list[Job]:
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    query = "engineer"
    if profile and getattr(profile, "target_titles", None):
        query = profile.target_titles[0]
    
    url = f"https://www.naukri.com/mnjuser/search/jobsearch/v2/search?noOfResults=20&keyword={query}&location=India"
    
    try:
        # Use StealthySession or force tier 2 to bypass Akamai
        resp = await router.fetch(url, min_tier=2, max_tier=2, purpose="job")
    except FetchError:
        return []
    return parse_naukri(resp.html or "", profile=profile)