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
    """Insert or update. Returns True if a new row was created."""
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


async def find_people_for_company(
    company: str,
    placeholder_domain: str,
    *,
    real_domain: str | None = None,
    llm_client=None,
    fetcher=None,
    walker_max_pages: int = 4,
    agent_max_pages: int = 3,
    agent_max_tokens: int = 15_000,
) -> tuple[list[PersonCandidate], str, str]:
    """Return (people, source, note)."""
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

    # Fallback to agent
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