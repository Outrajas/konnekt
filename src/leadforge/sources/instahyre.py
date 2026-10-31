"""Instahyre job source (India, ML-focused)."""
from __future__ import annotations

import json
from urllib.parse import quote

from leadforge.models import Job
from leadforge.seniority import infer_seniority


def _payload(raw: str | dict) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _domain_for_company(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in name.lower())
    slug = "-".join(filter(None, slug.split("-"))) or "unknown"
    return f"{slug}.instahyre"


def _build_description(item: dict) -> str:
    parts: list[str] = []
    employer = item.get("employer") or {}
    tagline = employer.get("company_tagline")
    if tagline:
        parts.append(str(tagline))
    note = employer.get("instahyre_note")
    if note:
        parts.append(str(note))
    keywords = item.get("keywords") or []
    if keywords:
        parts.append("Skills: " + ", ".join(str(k) for k in keywords))
    return "\n\n".join(parts)


def parse_instahyre(raw: str | dict, *, profile=None) -> list[Job]:
    data = _payload(raw)
    items = data.get("objects") or []
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("public_url") or "").strip()
        employer = item.get("employer") or {}
        company = (employer.get("company_name") or "").strip()
        if not (title and url and company):
            continue
        if url in seen:
            continue
        seen.add(url)
        location = item.get("locations") or ""
        if isinstance(location, list):
            location = ", ".join(str(x) for x in location)
        desc = _build_description(item)
        jobs.append(
            Job(
                company=company,
                domain=_domain_for_company(company),
                title=title,
                url=url,
                location=str(location) if location else None,
                description=desc,
                source="instahyre",
                seniority=infer_seniority(title, desc),
            )
        )
    return jobs


def _build_url(profile, limit: int) -> str:
    base = "https://www.instahyre.com/api/v1/job_search/"
    params: list[str] = [f"limit={limit}", "location=India"]
    if profile is not None:
        titles = getattr(profile, "target_titles", None) or []
        if titles:
            params.append(f"query={quote(titles[0])}")
        localities = getattr(profile, "localities", None) or []
        if localities and "India" not in localities:
            # Override if profile explicitly wants a different locality
            params = [f"limit={limit}", f"location={quote(localities[0])}"]
    return base + "?" + "&".join(params)


async def fetch_instahyre_jobs(profile, *, fetcher=None, limit: int = 50) -> list[Job]:
    """Fetch across ALL profile target_titles, deduplicated by URL."""
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    titles = (getattr(profile, "target_titles", None) or []) if profile else []
    if not titles:
        titles = [None]  # single global query if no titles

    seen: set[str] = set()
    jobs: list[Job] = []
    for title in titles:
        # Build URL for this title
        base = "https://www.instahyre.com/api/v1/job_search/"
        params = [f"limit={limit}", "location=India"]
        if title:
            from urllib.parse import quote
            params.append(f"query={quote(title)}")
        url = base + "?" + "&".join(params)
        try:
            resp = await router.fetch(url, min_tier=1, max_tier=1, purpose="job")
        except FetchError:
            continue
        for j in parse_instahyre(resp.html or "", profile=profile):
            if j.url in seen:
                continue
            seen.add(j.url)
            jobs.append(j)
    return jobs