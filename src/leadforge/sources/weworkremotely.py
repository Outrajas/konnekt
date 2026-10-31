"""We Work Remotely job source (RSS feed)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from leadforge.models import Job
from leadforge.seniority import infer_seniority


def parse_wwr(xml_text: str, *, profile=None) -> list[Job]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    jobs: list[Job] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        if title_el is None or link_el is None:
            continue
        title = (title_el.text or "").strip()
        url = (link_el.text or "").strip()
        if not title or not url or url in seen:
            continue
        seen.add(url)

        # WWR title format: "Company Name: Job Title"
        company = ""
        if ":" in title:
            parts = title.split(":", 1)
            company = parts[0].strip()
            title = parts[1].strip()

        desc = (desc_el.text or "") if desc_el is not None else ""
        slug = "".join(c if c.isalnum() else "-" for c in company.lower()).strip("-")
        domain = f"{slug}.wwr" if slug else "unknown.wwr"

        jobs.append(Job(
            company=company or "Unknown",
            domain=domain,
            title=title,
            url=url,
            location="Remote",
            description=desc[:8000],
            source="weworkremotely",
            remote=True,
            seniority=infer_seniority(title, desc),
        ))
    return jobs


async def fetch_wwr_jobs(profile, *, fetcher=None) -> list[Job]:
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher
    router = fetcher or Fetcher(settings)
    try:
        resp = await router.fetch(
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            min_tier=1, max_tier=1, purpose="job",
        )
    except FetchError:
        return []
    return parse_wwr(resp.html or "", profile=profile)