"""Remotive job source (remote, clean JSON)."""
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
    return f"{slug}.remotive"


def _strip_html(html: str) -> str:
    """Crude strip - just enough for salary/tech extraction downstream."""
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


_INDIA_FRIENDLY = {"india", "worldwide", "anywhere", "asia", "remote", "global"}


def _location_allows_india(loc: str) -> bool:
    """Return True if this location allows an India-based candidate."""
    if not loc:
        return True  # unstated = assume remote worldwide
    low = loc.lower()
    if "india" in low:
        return True
    for marker in _INDIA_FRIENDLY:
        if marker in low:
            return True
    return False


def parse_remotive(raw: str | dict, *, profile=None, india_only: bool = True) -> list[Job]:
    data = _payload(raw)
    items = data.get("jobs") or []
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        company = (item.get("company_name") or "").strip()
        if not (title and url and company):
            continue
        if url in seen:
            continue
        seen.add(url)

        salary = (item.get("salary") or "").strip()
        desc_text = _strip_html(item.get("description") or "")
        if salary:
            desc_text = f"Salary: {salary}. " + desc_text

        tags = item.get("tags") or []
        if tags:
            desc_text += " Tags: " + ", ".join(str(t) for t in tags)

        # India-eligibility gate
        loc_str = item.get("candidate_required_location") or ""
        if india_only and not _location_allows_india(loc_str):
            continue

        jobs.append(
            Job(
                company=company,
                domain=_domain_for_company(company),
                title=title,
                url=url,
                location=item.get("candidate_required_location") or None,
                employment_type=item.get("job_type") or None,
                description=desc_text[:8000],
                source="remotive",
                remote=True,
                seniority=infer_seniority(title, desc_text),
            )
        )
    return jobs


def _build_url(profile, limit: int) -> str:
    base = "https://remotive.com/api/remote-jobs"
    params: list[str] = [f"limit={limit}"]
    if profile is not None:
        titles = getattr(profile, "target_titles", None) or []
        if titles:
            params.append(f"search={quote(titles[0])}")
    return base + "?" + "&".join(params)


async def fetch_remotive_jobs(profile, *, fetcher=None, limit: int = 50) -> list[Job]:
    """Fetch across ALL profile target_titles, deduplicated by URL."""
    from urllib.parse import quote

    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    titles = (getattr(profile, "target_titles", None) or []) if profile else []
    if not titles:
        titles = [None]

    seen: set[str] = set()
    jobs: list[Job] = []
    for title in titles:
        base = "https://remotive.com/api/remote-jobs"
        params = [f"limit={limit}"]
        if title:
            params.append(f"search={quote(title)}")
        url = base + "?" + "&".join(params)
        try:
            resp = await router.fetch(url, min_tier=1, max_tier=1, purpose="job")
        except FetchError:
            continue
        for j in parse_remotive(resp.html or "", profile=profile):
            if j.url in seen:
                continue
            seen.add(j.url)
            jobs.append(j)
    return jobs