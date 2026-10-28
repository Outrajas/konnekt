"""End-to-end people discovery for companies with jobs.

For each company in the `job` table (filtered by profile), find people:
1. Resolve real domain (actual or from data/domain_overrides.json)
2. Try deterministic walker first (fast, cheap, deterministic)
3. Fall back to LLM-guided agent if walker returns 0
4. Persist PersonCandidate rows into the `person` table

Idempotent: running twice upserts by (company_id, first_name, last_name).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.models import PersonCandidate
from leadforge.sources.people import walk_company_people
from leadforge.sources.people_agent import find_people_agentic

log = logging.getLogger(__name__)


@dataclass
class CompanyRunResult:
    company: str
    placeholder_domain: str
    real_domain: str
    people_found: int
    persisted: int
    # walker | agent | override_missing | skip_placeholder | fetch_error
    source: str = "walker"
    note: str = ""


@dataclass
class PeopleE2EResult:
    companies: list[CompanyRunResult] = field(default_factory=list)
    total_found: int = 0
    total_persisted: int = 0


def load_domain_overrides(path: Path | None = None) -> dict[str, str]:
    """Load placeholder -> real domain map. Tolerates missing file and _keys."""
    if path is None:
        path = Path("data/domain_overrides.json")
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if not isinstance(v, str):
            continue
        out[k.strip().lower()] = v.strip().lower()
    return out


def _split_name(name: str) -> tuple[str, str]:
    """Split 'Jane Smith' -> ('Jane', 'Smith'). Multi-word middle names go to last."""
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _company_id(conn: sqlite3.Connection, domain: str, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM company WHERE domain=?", (domain,)
    ).fetchone()
    if row is not None:
        return cast(int, row["id"])
    now = clock.utcnow_iso()
    cur = conn.execute(
        "INSERT INTO company(name, domain, source, created_at) VALUES (?, ?, ?, ?)",
        (name, domain, "people-e2e", now),
    )
    return cast(int, cur.lastrowid)


def _upsert_person(
    conn: sqlite3.Connection,
    company_id: int,
    candidate: PersonCandidate,
    profile_id: str,
) -> bool:
    """Insert or update. Returns True if a new row was created.

    Belt-and-suspenders validation: even if the upstream parser produced a
    questionable name, refuse to persist anything that fails the strict
    name check. Protects against future parser regressions producing nav
    garbage that would clutter the person table.
    """
    from leadforge.sources.people import _has_role_keyword, _is_probable_name

    if not _is_probable_name(candidate.name):
        return False
    # A real person at a real company has a role. Nav headings don't.
    if not candidate.title or not _has_role_keyword(candidate.title):
        return False
    first, last = _split_name(candidate.name)
    if not first:
        return False
    now = clock.utcnow_iso()
    existing = conn.execute(
        "SELECT id FROM person WHERE company_id=? AND first_name=? AND last_name=?",
        (company_id, first, last),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE person SET title=COALESCE(NULLIF(?, ''), title), "
            "updated_at=? WHERE id=?",
            (candidate.title or "", now, cast(int, existing["id"])),
        )
        return False
    conn.execute(
        "INSERT INTO person (company_id, first_name, last_name, title, "
        "role_class, status, created_at, updated_at, profile_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            company_id,
            first,
            last,
            candidate.title or "",
            "other",
            "new",
            now,
            now,
            profile_id,
        ),
    )
    return True


async def find_people_via_search(
    company: str,
    domain: str,
    *,
    settings,
    fetcher=None,
    max_results: int = 15,
) -> list[PersonCandidate]:
    """Search for public profiles; parse LinkedIn-style snippets into candidates.

    Provider preference (matches cli.py's `people` command):
      1. SearxNG  - if settings.searxng_base_url is set (reliable)
      2. DDG      - as fallback (often bot-checked in 2026; we surface
                    the warning and return [] so the caller can try agent)

    Errors are logged (not swallowed silently) so failures are diagnosable.
    """
    import logging as _logging

    from leadforge.sources.search import (
        DuckDuckGoProvider,
        SearxngProvider,
        dedupe_people,
        parse_person_snippet,
        person_query,
    )
    from leadforge.web.fetcher import Fetcher

    log = _logging.getLogger(__name__)

    try:
        query = person_query(company, domain, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("person_query failed for %r: %s: %s", company, type(exc).__name__, exc)
        return []
    if not query:
        return []

    router = fetcher or Fetcher(settings)

    # Build provider list: SearxNG first if configured, DDG as fallback.
    # Explicit type annotation: both implementations satisfy SearchProvider.
    from leadforge.sources.search import SearchProvider as _SearchProvider
    providers: list[tuple[str, _SearchProvider]] = []
    searxng_url = getattr(settings, "searxng_base_url", None)
    if searxng_url:
        try:
            providers.append(("searxng", SearxngProvider(searxng_url, fetcher=router)))
        except Exception as exc:  # noqa: BLE001
            log.warning("SearxngProvider construction failed: %s", exc)
    try:
        providers.append(("ddg", DuckDuckGoProvider(fetcher=router)))
    except Exception as exc:  # noqa: BLE001
        log.warning("DuckDuckGoProvider construction failed: %s", exc)

    if not providers:
        log.warning("no search provider available")
        return []

    hits: list = []
    used_provider = ""
    for name, provider in providers:
        try:
            candidate_hits = await provider.search(query)
        except ValueError as exc:
            # Provider raises this on unrecognised markup (bot-check page).
            log.warning(
                "%s search returned unrecognised markup for %r: %s "
                "(likely bot-check; try SearxNG: set LEADFORGE_SEARXNG_BASE_URL)",
                name, company, exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "%s search failed for %r: %s: %s",
                name, company, type(exc).__name__, exc,
            )
            continue
        if candidate_hits:
            hits = candidate_hits
            used_provider = name
            break

    if not hits:
        log.info("all search providers returned no hits for %r", company)
        return []

    log.info("%s returned %d hits for %r", used_provider, len(hits), company)

    people: list[PersonCandidate] = []
    for hit in hits:
        try:
            parsed = parse_person_snippet(hit, expected_company=company)
        except Exception:  # noqa: BLE001
            continue
        if parsed is not None:
            people.append(parsed)

    if not people:
        log.info(
            "search found %d hits but 0 matched person pattern for %r",
            len(hits), company,
        )
        return []

    try:
        return dedupe_people(people)[:max_results]
    except Exception:  # noqa: BLE001
        return people[:max_results]


async def find_people_for_company(
    company: str,
    placeholder_domain: str,
    *,
    real_domain: str | None = None,
    llm_client=None,
    fetcher=None,
    settings=None,
    use_search: bool = True,
    use_llm: bool = True,
    walker_max_pages: int = 4,
    agent_max_pages: int = 3,
    agent_max_tokens: int = 15_000,
) -> tuple[list[PersonCandidate], str, str]:
    """Return (people, source, note).

    Path order:
    1. walker  (deterministic team-page parse; cheap)
    2. search  (DDG dorks for LinkedIn / public profiles; cheap)
    3. agent   (LLM-guided recursive crawl; expensive)
    """
    # Resolve domain
    dom = (real_domain or placeholder_domain or "").strip().lower()
    if not dom or dom.endswith(".wellfound"):
        if real_domain:
            dom = real_domain.strip().lower()
        else:
            return [], "override_missing", f"placeholder {placeholder_domain!r} has no real domain"

    try:
        walker_people = await walk_company_people(
            company, dom, fetcher=fetcher, max_pages=walker_max_pages
        )
    except Exception as exc:  # noqa: BLE001
        return [], "fetch_error", f"walker: {type(exc).__name__}: {exc}"

    if walker_people:
        return walker_people, "walker", ""

    # Path 2: web search (cheap, uses DDG dorks)
    if use_search and settings is not None:
        try:
            search_people = await find_people_via_search(
                company, dom, settings=settings, fetcher=fetcher, max_results=15
            )
        except Exception:  # noqa: BLE001
            search_people = []
        if search_people:
            return search_people, "search", f"{len(search_people)} from DDG"

    # Path 3: LLM-guided agent (expensive)
    if not use_llm:
        return [], "no_results", "walker and search empty; LLM disabled"

    try:
        result = await find_people_agentic(
            company=company,
            start_url=f"https://{dom}/about",
            goal="find HR, talent acquisition, or engineering managers",
            llm_client=llm_client,
            fetcher=fetcher,
            max_pages=agent_max_pages,
            max_tokens=agent_max_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        return [], "fetch_error", f"agent: {type(exc).__name__}: {exc}"

    return result.people, "agent", result.stop_reason


async def run_people_e2e(
    settings: Settings,
    *,
    profile_id: str = "ai_ml",
    max_companies: int | None = 5,
    domain_overrides_path: Path | None = None,
    explicit_domains: dict[str, str] | None = None,
    dry_run: bool = False,
    llm_client=None,
    fetcher=None,
    use_search: bool = True,
    use_llm: bool = True,
) -> PeopleE2EResult:
    """Orchestrate: jobs -> companies -> people -> person table."""
    overrides = load_domain_overrides(domain_overrides_path)
    if explicit_domains:
        overrides.update({k.lower(): v.lower() for k, v in explicit_domains.items()})

    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        # Get distinct companies with jobs for this profile
        rows = conn.execute(
            "SELECT DISTINCT c.id, c.name, c.domain FROM job j "
            "JOIN company c ON c.id=j.company_id "
            "WHERE j.profile_id=? OR j.profile_id IS NULL "
            "ORDER BY c.name",
            (profile_id,),
        ).fetchall()
        if max_companies is not None:
            rows = rows[:max_companies]
    finally:
        conn.close()

    result = PeopleE2EResult()

    # Dry run: when no fetcher is injected, skip network entirely and just
    # report what would be processed. When the caller injects a fetcher
    # (tests, replay), run the full path but skip DB persistence.
    if dry_run and fetcher is None:
        for row in rows:
            placeholder = (row["domain"] or "").strip().lower()
            real_domain = overrides.get(placeholder, "")
            result.companies.append(
                CompanyRunResult(
                    company=row["name"] or "?",
                    placeholder_domain=placeholder,
                    real_domain=real_domain or placeholder,
                    people_found=0,
                    persisted=0,
                    source="dry_run",
                    note="dry run - no network, no persist",
                )
            )
        return result

    for row in rows:
        comp_name = row["name"] or "?"
        placeholder = (row["domain"] or "").strip().lower()
        real_domain = overrides.get(placeholder, "")

        people, source, note = await find_people_for_company(
            comp_name,
            placeholder,
            real_domain=real_domain,
            llm_client=llm_client,
            fetcher=fetcher,
            settings=settings,
            use_search=use_search,
            use_llm=use_llm,
        )

        persisted = 0
        if people and not dry_run:
            conn = connect(settings.db_path)
            try:
                cid = _company_id(conn, real_domain or placeholder, comp_name)
                for cand in people:
                    if _upsert_person(conn, cid, cand, profile_id):
                        persisted += 1
                conn.commit()
            finally:
                conn.close()

        result.companies.append(
            CompanyRunResult(
                company=comp_name,
                placeholder_domain=placeholder,
                real_domain=real_domain or placeholder,
                people_found=len(people),
                persisted=persisted,
                source=source,
                note=note,
            )
        )
        result.total_found += len(people)
        result.total_persisted += persisted

    return result