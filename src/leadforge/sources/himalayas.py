"""Himalayas job source (remote, JSON, cursor pagination, structured salary)."""
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
    return f"{slug}.himalayas"


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _url_for_item(item: dict) -> str:
    slug = item.get("companySlug") or ""
    guid = item.get("guid") or item.get("id") or ""
    if slug and guid:
        return f"https://himalayas.app/companies/{slug}/jobs/{guid}"
    if slug:
        return f"https://himalayas.app/companies/{slug}"
    return ""


def parse_himalayas(raw: str | dict, *, profile=None) -> list[Job]:
    data = _payload(raw)
    items = data.get("jobs") or []
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        company = (item.get("companyName") or "").strip()
        if not (title and company):
            continue
        url = _url_for_item(item)
        if not url or url in seen:
            continue
        seen.add(url)

        desc_text = _strip_html(item.get("description") or "")
        excerpt = (item.get("excerpt") or "").strip()
        if excerpt:
            desc_text = excerpt + " " + desc_text

        # Structured salary → prefix so extract_salary picks it up
        lo = item.get("minSalary")
        hi = item.get("maxSalary")
        cur = item.get("currency") or ""
        period = item.get("salaryPeriod") or "year"
        if lo is not None or hi is not None:
            lo_s = f"{lo}" if lo is not None else ""
            hi_s = f"{hi}" if hi is not None else ""
            rng = f"{lo_s}-{hi_s}" if hi_s and lo_s else (lo_s or hi_s)
            desc_text = f"Salary: {cur}{rng} per {period}. " + desc_text

        restrictions = item.get("locationRestrictions") or []
        location = ", ".join(str(x) for x in restrictions) if restrictions else "Remote"

        seniority = item.get("seniority") or []
        if seniority:
            desc_text += " Seniority: " + ", ".join(str(s) for s in seniority)

        jobs.append(
            Job(
                company=company,
                domain=_domain_for_company(company),
                title=title,
                url=url,
                location=location,
                employment_type=item.get("employmentType") or None,
                description=desc_text[:8000],
                source="himalayas",
                remote=True,
                seniority=infer_seniority(title, desc_text),
            )
        )
    return jobs


def _build_url(profile, limit: int) -> str:
    base = "https://himalayas.app/jobs/api"
    params: list[str] = [f"limit={limit}"]
    if profile is not None:
        titles = getattr(profile, "target_titles", None) or []
        if titles:
            params.append(f"query={quote(titles[0])}")
    return base + "?" + "&".join(params)


async def fetch_himalayas_jobs(profile, *, fetcher=None, limit: int = 50) -> list[Job]:
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    try:
        resp = await router.fetch(
            _build_url(profile, limit),
            min_tier=1, max_tier=1, purpose="job",
        )
    except FetchError:
        return []
    return parse_himalayas(resp.html or "", profile=profile)