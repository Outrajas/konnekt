"""YC Work at a Startup source."""
from __future__ import annotations

import json

from leadforge.models import Job
from leadforge.seniority import infer_seniority


def parse_yc(raw: str | dict, *, profile=None) -> list[Job]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        data = raw

    # YC Algolia returns hits with job data
    hits = data.get("hits", []) if isinstance(data, dict) else []
    jobs: list[Job] = []
    for hit in hits:
        title = hit.get("title") or hit.get("job_title") or ""
        company = hit.get("company_name") or hit.get("company") or "YC Startup"
        url = hit.get("url") or ""
        if not (title and url):
            continue
        desc = hit.get("description") or ""
        slug = "".join(c if c.isalnum() else "-" for c in company.lower()).strip("-")
        jobs.append(Job(
            company=company,
            domain=f"{slug}.yc" if slug else "unknown.yc",
            title=title,
            url=url,
            location=hit.get("location") or "Remote",
            description=desc[:8000],
            source="yc-startup",
            remote=True,
            seniority=infer_seniority(title, desc),
        ))
    return jobs


async def fetch_yc_jobs(profile, *, fetcher=None) -> list[Job]:
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher
    router = fetcher or Fetcher(settings)
    # Use a specific Algolia query or the YC API
    try:
        resp = await router.fetch(
            "https://45bwzj1sbz-dsn.algolia.net/1/indexes/*/queries",
            method="POST",
            json={"requests":[{"indexName":"jobs","params":"query=engineer"}]},
            min_tier=1, max_tier=1, purpose="job",
        )
    except FetchError:
        return []
    return parse_yc(resp.html or "", profile=profile)