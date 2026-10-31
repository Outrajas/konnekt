"""Shared parsing for the JavaScript-heavy job platforms.

LinkedIn, Indeed and Glassdoor all serve the same three shapes:

1. **JSON-LD** â€” ``<script type="application/ld+json">`` with a ``JobPosting``
   (sometimes nested in ``@graph``). This is the most stable surface they have,
   because they emit it for Google Jobs.
2. **Anchor cards** â€” ``/jobs/view/<id>`` (LinkedIn), ``/viewjob?jk=`` (Indeed),
   ``/job-listing/`` (Glassdoor). Titles are in the anchor text or a heading.
3. **A JSON blob in the page** â€” Indeed embeds job-card JSON in the HTML.

Everything here is pure and testable offline; the network lives in the three
small source modules that call :func:`fetch_platform`.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from leadforge.models import Job
from leadforge.seniority import infer_seniority
from leadforge.web.errors import FetchError

PLATFORM_PURPOSES = frozenset({"job", "hiring-signal", "person", "profile", "search-snippet-miss"})
JOB_ID = re.compile(r"(?:/jobs/view/|/viewjob\?jk=|/job-listing/)([A-Za-z0-9_-]{4,})")

def _text(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@id") or ""
    return " ".join(str(value).split()) if value else ""


def _location(posting: dict) -> str:
    raw = posting.get("jobLocation")
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        return ""
    address = raw.get("address") if isinstance(raw.get("address"), dict) else raw
    parts = [
        _text(address.get("addressLocality")),
        _text(address.get("addressRegion")),
        _text(address.get("addressCountry")),
    ]
    return ", ".join(part for part in parts if part)


def _iter_postings(node: object):
    if isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(item).lower() == "jobposting" for item in kinds):
            yield node
        for value in node.values():
            yield from _iter_postings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_postings(item)


def job_postings_from_jsonld(html: str, source: str, *, fallback_company: str = "") -> list[Job]:
    """Extract schema.org JobPosting records from every ld+json block on a page."""
    soup = BeautifulSoup(html, "lxml")
    jobs: list[Job] = []
    for block in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = block.string or block.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for posting in _iter_postings(data):
            title = _text(posting.get("title"))
            company = _text(posting.get("hiringOrganization")) or fallback_company
            url = _text(posting.get("url"))
            if not title or not company or not url:
                continue
            description = _text(posting.get("description"))[:8000]
            employment = posting.get("employmentType")
            if isinstance(employment, list):
                employment = employment[0] if employment else ""
            jobs.append(
                Job(
                    company=company,
                    domain=urlsplit(url).netloc,
                    title=title,
                    url=url,
                    location=_location(posting),
                    employment_type=_text(employment),
                    description=description,
                    source=source,
                    seniority=infer_seniority(title, description),
                    remote="remote" in (title + " " + _location(posting)).lower(),
                )
            )
    return jobs


def parse_anchor_cards(
    html: str,
    *,
    source: str,
    base_url: str,
    anchor_pattern: str = r"/jobs/view/",
    fallback_company: str = "",
) -> list[Job]:
    """Last-resort card parser: anchors that look like job links, deduped by URL."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    jobs: list[Job] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not re.search(anchor_pattern, href):
            continue
        url = urljoin(base_url, href).split("?")[0]
        if url in seen or not JOB_ID.search(url) and "job" not in url.lower():
            continue
        seen.add(url)
        title = " ".join((anchor.get_text(" ") or anchor.get("aria-label") or "").split())
        if len(title) < 4:
            container = anchor.find_parent(["li", "div", "article"])
            heading = container.find(["h2", "h3", "h4"]) if container else None
            title = " ".join((heading.get_text(" ") if heading else "").split())
        if len(title) < 4:
            continue
        jobs.append(
            Job(
                company=fallback_company or _guess_company(anchor),
                domain=urlsplit(url).netloc,
                title=title[:200],
                url=url,
                source=source,
                seniority=infer_seniority(title, ""),
            )
        )
    return jobs


def _guess_company(anchor) -> str:
    parent = anchor.find_parent(["li", "div", "article"])
    if parent is None:
        return ""
    for node in parent.find_all(["span", "div", "a"]):
        text = " ".join(node.get_text(" ").split())
        if 2 < len(text) < 60 and node is not anchor:
            return text
    return ""


def parse_platform_page(
    html: str,
    *,
    source: str,
    base_url: str,
    anchor_pattern: str,
    fallback_company: str = "",
) -> list[Job]:
    jobs = job_postings_from_jsonld(html, source, fallback_company=fallback_company)
    if jobs:
        return jobs
    return parse_anchor_cards(
        html,
        source=source,
        base_url=base_url,
        anchor_pattern=anchor_pattern,
        fallback_company=fallback_company,
    )


async def fetch_platform(fetcher, url: str, *, purpose: str = "job"):
    """Fetch a platform page at Tier 3 when the browser is on, else Tier 1."""
    settings = getattr(fetcher, "settings", None)
    tier3 = bool(getattr(settings, "browser_enabled", False))
    if tier3:
        try:
            return await fetcher.fetch(url, min_tier=3, max_tier=3, purpose=purpose)
        except FetchError:
            # Browser unavailable or refused by policy: fall back once to HTTP.
            return await fetcher.fetch(url, min_tier=1, max_tier=1, purpose=purpose)
    return await fetcher.fetch(url, min_tier=1, max_tier=1, purpose=purpose)


def search_terms(profile, limit: int = 3) -> list[str]:
    titles = getattr(profile, "target_titles", None) or []
    cleaned = [title.strip() for title in titles if str(title).strip()]
    return cleaned[:limit] or ["software engineer"]
