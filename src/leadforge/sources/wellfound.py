"""Wellfound (formerly AngelList Talent) job source.

Wellfound is a Next.js app. Job listings live inside the `__NEXT_DATA__`
script tag. The parser is deliberately defensive because Wellfound
periodically renames internal schema keys.

This module handles only parsing. The live fetcher lives in a follow-up
commit once a real response fixture is captured.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from leadforge.models import Job

WELLFOUND_SEARCH_URL = "https://wellfound.com/jobs"

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)

_SALARY_KEYS = ("compensation", "salary", "compensationRange", "salaryRange")


def _extract_next_data(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _domain_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _compensation_tuple(item: dict) -> tuple[int | None, int | None, str | None]:
    for key in _SALARY_KEYS:
        block = item.get(key)
        if isinstance(block, dict):
            lo = _to_int(block.get("min") or block.get("minAmount"))
            hi = _to_int(block.get("max") or block.get("maxAmount"))
            cur = block.get("currency") or block.get("currencyCode")
            if lo or hi:
                return lo, hi, str(cur) if cur else None
    return None, None, None


def _salary_prefix(lo: int | None, hi: int | None, cur: str | None) -> str:
    """Build a string the salary extractor will pick up from description."""
    if not (lo or hi):
        return ""
    c = cur or ""
    if lo and hi and lo != hi:
        return f"Salary: {c}{lo}-{hi} per year. "
    amt = lo or hi
    return f"Salary: {c}{amt} per year. "


def _location_of(item: dict) -> str:
    loc = item.get("location") or item.get("locationName") or ""
    if isinstance(loc, dict):
        loc = loc.get("name") or loc.get("displayName") or ""
    if not loc and item.get("remote"):
        loc = "Remote"
    return str(loc) if loc else ""


def _description_of(item: dict) -> str:
    desc = item.get("description") or item.get("descriptionText") or ""
    if isinstance(desc, dict):
        desc = desc.get("text") or desc.get("html") or ""
    return str(desc)


def _company_and_domain(item: dict) -> tuple[str, str]:
    startup = item.get("startup") or item.get("company") or {}
    if isinstance(startup, str):
        return startup, ""
    if not isinstance(startup, dict):
        return "", ""
    name = startup.get("name") or startup.get("displayName") or item.get("companyName") or ""
    website = startup.get("website") or ""
    domain = _domain_from_url(website)
    if not domain:
        slug = startup.get("slug") or startup.get("companySlug") or ""
        if slug:
            domain = f"{slug}.wellfound"
    return str(name), domain


def _job_url(item: dict) -> str:
    url = item.get("url") or item.get("jobUrl") or ""
    if url:
        return str(url)
    slug = item.get("slug") or ""
    jid = item.get("id")
    if jid and slug:
        return f"https://wellfound.com/jobs/{jid}-{slug}"
    if slug:
        return f"https://wellfound.com/jobs/{slug}"
    if jid:
        return f"https://wellfound.com/jobs/{jid}"
    return ""


def _walk_jobs_list(next_data: dict) -> list:
    """Wellfound has shipped several shapes; try each."""
    props = next_data.get("props") or {}
    page = props.get("pageProps") or {}
    for key in ("jobs", "jobListings", "initialJobListings", "listings"):
        candidate = page.get(key)
        if isinstance(candidate, list):
            return candidate
    state = page.get("initialState") or {}
    if isinstance(state, dict):
        for key in ("jobs", "jobListings", "listings"):
            candidate = state.get(key)
            if isinstance(candidate, list):
                return candidate
    # Newer shape: pageProps.data.jobs
    data = page.get("data") or {}
    if isinstance(data, dict):
        for key in ("jobs", "jobListings", "listings"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def _job_from_item(item: dict) -> Job | None:
    if not isinstance(item, dict):
        return None
    title = item.get("title") or item.get("role") or ""
    if not title:
        return None
    company, domain = _company_and_domain(item)
    if not company or not domain:
        return None
    url = _job_url(item)
    if not url:
        return None

    lo, hi, cur = _compensation_tuple(item)
    desc = _salary_prefix(lo, hi, cur) + _description_of(item)

    return Job(
        company=company,
        domain=domain,
        title=str(title),
        url=url,
        location=_location_of(item) or None,
        employment_type=item.get("jobType") or item.get("employmentType"),
        description=desc,
        source="wellfound",
    )


def parse_wellfound(html: str) -> list[Job]:
    """Parse Wellfound HTML. Returns [] when the payload shape is unrecognised."""
    next_data = _extract_next_data(html)
    if not next_data:
        return []

    items = _walk_jobs_list(next_data)
    if not items:
        return []

    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        job = _job_from_item(item)
        if job is None or job.url in seen:
            continue
        seen.add(job.url)
        jobs.append(job)
    return jobs


def build_search_url(profile=None) -> str:
    """Return the search URL.

    Placeholder: verify against live Wellfound before enabling the fetcher.
    A profile-aware version would append role/location query params.
    """
    return WELLFOUND_SEARCH_URL