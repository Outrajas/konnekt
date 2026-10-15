"""People-search interfaces with routed live and deterministic replay providers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from leadforge.config import Settings
from leadforge.models import PersonCandidate, SearchHit
from leadforge.web.fetcher import Fetcher


class SearchProvider(Protocol):
    async def search(self, query: str) -> list[SearchHit]: ...


class SearxngProvider:
    """Query an operator-hosted SearxNG JSON endpoint through the router."""

    def __init__(self, base_url: str, *, fetcher: Fetcher | None = None) -> None:
        self.base_url = base_url
        self.fetcher = fetcher or Fetcher()

    async def search(self, query: str) -> list[SearchHit]:
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("SearxNG URL must be an absolute HTTP(S) URL")
        params = dict(parse_qsl(parts.query))
        params.update(format="json", q=query)
        url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path or "/search", urlencode(params), "")
        )
        result = await self.fetcher.fetch(url, min_tier=1, max_tier=1, purpose="search")
        payload = json.loads(result.html)
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        hits = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("title") or not row.get("url"):
                continue
            hits.append(
                SearchHit(
                    title=str(row["title"]),
                    url=str(row["url"]),
                    snippet=str(row.get("content") or ""),
                )
            )
        return hits


class ReplaySearchProvider:
    """Search provider that reads a local fixture only and never accesses the network."""

    def __init__(self, directory: Path) -> None:
        fixture = directory / "search.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        rows = payload.get("hits", []) if isinstance(payload, dict) else []
        self.hits = [SearchHit.model_validate(row) for row in rows]

    async def search(self, query: str) -> list[SearchHit]:
        return list(self.hits)


def parse_person_snippet(hit: SearchHit, expected_company: str = "") -> PersonCandidate | None:
    """Parse only the search result's title/snippet; never fetch the result URL."""
    text = " ".join(f"{hit.title} {hit.snippet}".split())
    text = re.sub(r"\s*\|\s*(LinkedIn|X|Twitter)\s*$", "", text, flags=re.IGNORECASE)
    match = re.search(
        r"(?P<name>[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3})"
        r"\s*(?:[-–—|])\s*(?P<title>[^|.]{3,100}?)\s+(?:at|@)\s+"
        r"(?P<company>[A-Z][A-Za-z0-9& .'-]{1,60})",
        text,
    )
    if not match:
        return None
    company = expected_company or match.group("company").strip()
    return PersonCandidate(
        name=match.group("name").strip(),
        title=match.group("title").strip(),
        company=company,
        url=hit.url,
        snippet=hit.snippet,
        source="search-snippet",
    )


def dedupe_people(people: list[PersonCandidate]) -> list[PersonCandidate]:
    seen: set[tuple[str, str]] = set()
    output = []
    for person in people:
        key = (_normalize(person.name), _normalize(person.company))
        if key not in seen:
            seen.add(key)
            output.append(person)
    return output


def person_query(company: str, domain: str, settings: Settings) -> str:
    template = (
        'site:linkedin.com/in "{company}" '
        '(recruiter OR "talent acquisition" OR "engineering manager")'
    )
    query_path = Path("data/queries.toml")
    if query_path.is_file():
        import tomllib

        data = tomllib.loads(query_path.read_text(encoding="utf-8"))
        template = str(data.get("people", {}).get("query", template))
    return template.format(company=company, domain=domain)


def _normalize(value: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())
