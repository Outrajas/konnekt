"""RemoteOK job source (remote, JSON array, skip legal item[0])."""
from __future__ import annotations

import json

from leadforge.models import Job
from leadforge.seniority import infer_seniority


def _payload(raw: str | list) -> list:
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _domain_for_company(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in name.lower())
    slug = "-".join(filter(None, slug.split("-"))) or "unknown"
    return f"{slug}.remoteok"


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_remoteok(raw: str | list, *, profile=None) -> list[Job]:
    data = _payload(raw)
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        # Skip legal disclaimer item
        if "legal" in item or "last_updated" in item and "position" not in item:
            continue

        title = (item.get("position") or item.get("title") or "").strip()
        company = (item.get("company") or "").strip()
        slug = item.get("slug") or item.get("id") or ""
        if not (title and company and slug):
            continue
        url = f"https://remoteok.com/remote-jobs/{slug}"
        if url in seen:
            continue
        seen.add(url)

        desc_text = _strip_html(item.get("description") or "")

        # Salary as structured fields (when present)
        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        if salary_min and salary_max:
            desc_text = (
                f"Salary: ${salary_min}-${salary_max} per year. " + desc_text
            )

        tags = item.get("tags") or []
        if tags:
            desc_text += " Tags: " + ", ".join(str(t) for t in tags)

        jobs.append(
            Job(
                company=company,
                domain=_domain_for_company(company),
                title=title,
                url=url,
                location=item.get("location") or "Remote",
                description=desc_text[:8000],
                source="remoteok",
                remote=True,
                seniority=infer_seniority(title, desc_text),
            )
        )
    return jobs


async def fetch_remoteok_jobs(profile, *, fetcher=None, limit: int = 100) -> list[Job]:
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    try:
        resp = await router.fetch(
            "https://remoteok.com/api",
            min_tier=1, max_tier=1, purpose="job",
        )
    except FetchError:
        return []
    jobs = parse_remoteok(resp.html or "", profile=profile)
    return jobs[:limit]