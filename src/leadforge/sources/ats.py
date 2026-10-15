"""Parsers for public ATS job-board JSON feeds."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from leadforge.models import Job
from leadforge.web.fetcher import Fetcher

FetchFn = Callable[[str], Awaitable[str]]


def parse_greenhouse(payload: str | dict, company: str, domain: str) -> list[Job]:
    data = _payload(payload)
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    output = []
    for item in jobs:
        if not isinstance(item, dict) or not item.get("title") or not item.get("absolute_url"):
            continue
        location = item.get("location")
        if isinstance(location, dict):
            location = location.get("name")
        output.append(
            Job(
                company=company,
                domain=domain,
                title=str(item["title"]),
                url=str(item["absolute_url"]),
                location=str(location) if location else None,
                description=str(item.get("content") or ""),
                source="greenhouse",
            )
        )
    return output


def parse_lever(payload: str | list, company: str, domain: str) -> list[Job]:
    data = _payload(payload)
    jobs = data if isinstance(data, list) else []
    output = []
    for item in jobs:
        if not isinstance(item, dict) or not item.get("text") or not item.get("hostedUrl"):
            continue
        categories = item.get("categories") or {}
        output.append(
            Job(
                company=company,
                domain=domain,
                title=str(item["text"]),
                url=str(item["hostedUrl"]),
                location=_optional(categories.get("location")),
                employment_type=_optional(categories.get("commitment")),
                description=str(
                    item.get("descriptionPlain") or item.get("descriptionBodyPlain") or ""
                ),
                source="lever",
            )
        )
    return output


def parse_ashby(payload: str | dict, company: str, domain: str) -> list[Job]:
    data = _payload(payload)
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    output = []
    for item in jobs:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        url = item.get("jobUrl") or item.get("applyUrl")
        if not url:
            continue
        output.append(
            Job(
                company=company,
                domain=domain,
                title=str(item["title"]),
                url=str(url),
                location=_optional(item.get("location")),
                employment_type=_optional(item.get("employmentType")),
                description=str(item.get("descriptionPlain") or ""),
                source="ashby",
            )
        )
    return output


async def fetch_jobs(
    ats: str, board: str, company: str, domain: str, *, fetcher: Fetcher | None = None
) -> list[Job]:
    """Fetch a known public board only through the shared policy router."""
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{board}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{board}",
    }
    parsers = {
        "greenhouse": parse_greenhouse,
        "lever": parse_lever,
        "ashby": parse_ashby,
    }
    if ats not in urls:
        raise ValueError(f"Unsupported ATS: {ats}")
    response = await (fetcher or Fetcher()).fetch(urls[ats], min_tier=1, max_tier=1, purpose="job")
    return parsers[ats](response.html, company, domain)


def detect_ats_link(urls: list[str]) -> tuple[str, str] | None:
    """Find a public job-board slug in careers-page links."""
    host_map = {
        "boards.greenhouse.io": "greenhouse",
        "jobs.lever.co": "lever",
        "jobs.ashbyhq.com": "ashby",
    }
    for url in urls:
        parsed = urlparse(url)
        ats = host_map.get((parsed.hostname or "").lower())
        board = next((part for part in parsed.path.split("/") if part), None)
        if ats and board:
            return ats, board
    return None


def _payload(payload):
    return json.loads(payload) if isinstance(payload, str) else payload


def _optional(value) -> str | None:
    return str(value).strip() if value else None
