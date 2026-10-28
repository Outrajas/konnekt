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


class DuckDuckGoProvider:
    """Logged-out Tier 1 HTML search through the policy router."""

    def __init__(self, *, fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher or Fetcher()

    async def search(self, query: str) -> list[SearchHit]:
        url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
        page = await self.fetcher.fetch(
            url, min_tier=1, max_tier=1, purpose="search", force_httpx=True
        )
        return parse_duckduckgo_results(page.html)


def parse_duckduckgo_results(markup: str) -> list[SearchHit]:
    """Parse the result-card markup, tolerating known class variations."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")
    results = soup.select(".result, [data-testid='result'], .web-result")
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for result in results:
        title_node = result.select_one(".result__a, a[data-testid='result-title-a'], h2 a")
        if title_node is None:
            continue
        title = " ".join(title_node.stripped_strings)
        url = _unwrap_duckduckgo_url(str(title_node.get("href") or ""))
        snippet_node = result.select_one(
            ".result__snippet, [data-testid='result-snippet'], .result__extras__snippet"
        )
        snippet = " ".join(snippet_node.stripped_strings) if snippet_node else ""
        if not title or not url or url in seen:
            continue
        seen.add(url)
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
    if not hits:
        raise ValueError("unrecognised DuckDuckGo results markup")
    return hits


def _unwrap_duckduckgo_url(value: str) -> str | None:
    if value.startswith("//"):
        value = "https:" + value
    parts = urlsplit(value)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com"):
        query = dict(parse_qsl(parts.query))
        value = query.get("uddg", "")
    target = urlsplit(value)
    return value if target.scheme in {"http", "https"} and target.hostname else None


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
    """Parse only the search result's title/snippet; never fetch the result URL.

    Two patterns accepted (LinkedIn uses both):
      "Jane Smith - Head of Talent at Augury"
      "Jane Smith - Head of Talent - Augury | LinkedIn"
    """
    text = " ".join(f"{hit.title} {hit.snippet}".split())
    text = re.sub(r"\s*\|\s*(LinkedIn|X|Twitter)\s*$", "", text, flags=re.IGNORECASE)

    # Pattern 1 (preferred): name - title at Company
    match = re.search(
        r"(?P<name>[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3})"
        r"\s*[-–—|]\s*(?P<title>[^|.]{3,100}?)\s+(?:at|@)\s+"
        r"(?P<company>[A-Z][A-Za-z0-9& .'-]{1,60})",
        text,
    )
    if match:
        company = expected_company or match.group("company").strip()
        return PersonCandidate(
            name=match.group("name").strip(),
            title=match.group("title").strip(),
            company=company,
            url=hit.url,
            snippet=hit.snippet,
            source="search-snippet",
        )

    # Pattern 2 (fallback): name - title - Company [| LinkedIn]
    if expected_company:
        match = re.search(
            r"(?P<name>[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3})"
            r"\s*[-–—|]\s*(?P<title>[^|.]{3,80}?)\s*[-–—|]\s*"
            + re.escape(expected_company)
            + r"\b",
            text,
            re.IGNORECASE,
        )
        if match:
            return PersonCandidate(
                name=match.group("name").strip(),
                title=match.group("title").strip(),
                company=expected_company,
                url=hit.url,
                snippet=hit.snippet,
                source="search-snippet",
            )

    return None


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
