"""Unstop job source (India entry-level, JSON)."""
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
    return f"{slug}.unstop"


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _url_for_item(item: dict) -> str:
    path = item.get("public_url") or ""
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return f"https://unstop.com/{path.lstrip('/')}"


def parse_unstop(raw: str | dict, *, profile=None) -> list[Job]:
    data = _payload(raw)
    inner = data.get("data") or {}
    items = inner.get("data") if isinstance(inner, dict) else []
    if not isinstance(items, list):
        items = []
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        url = _url_for_item(item)
        if not url or url in seen:
            continue
        seen.add(url)

        org = item.get("organisation") or {}
        company = (org.get("name") or item.get("organization_name") or "").strip()
        if not company:
            # Fallback: use public_url slug segment
            parts = url.rstrip("/").split("/")[-1].split("-")
            company = " ".join(p.title() for p in parts[-3:-1] if p) or "Unknown"
        if not company:
            continue

        desc_text = _strip_html(item.get("details") or "")
        region = item.get("region") or ""
        # India-eligibility: unstop is India-focused; skip anything region='other'
        if region and region.lower() not in ("online", "india", "in-person", "offline"):
            if "india" not in region.lower():
                continue

        jobs.append(
            Job(
                company=company,
                domain=_domain_for_company(company),
                title=title,
                url=url,
                location=region or None,
                description=desc_text[:8000],
                source="unstop",
                seniority=infer_seniority(title, desc_text),
            )
        )
    return jobs


def _build_url(profile, limit: int) -> str:
    base = "https://unstop.com/api/public/opportunity/search-result"
    params: list[str] = ["opportunity=jobs", f"per_page={limit}"]
    if profile is not None:
        titles = getattr(profile, "target_titles", None) or []
        if titles:
            params.append(f"searchTerm={quote(titles[0])}")
    return base + "?" + "&".join(params)


async def fetch_unstop_jobs(profile, *, fetcher=None, limit: int = 20) -> list[Job]:
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
        base = "https://unstop.com/api/public/opportunity/search-result"
        params = ["opportunity=jobs", f"per_page={limit}"]
        if title:
            params.append(f"searchTerm={quote(title)}")
        url = base + "?" + "&".join(params)
        try:
            resp = await router.fetch(url, min_tier=1, max_tier=1, purpose="job")
        except FetchError:
            continue
        for j in parse_unstop(resp.html or "", profile=profile):
            if j.url in seen:
                continue
            seen.add(j.url)
            jobs.append(j)
    return jobs