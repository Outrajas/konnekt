"""Persist Job models to the database. Idempotent upsert by URL."""
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.models import Job


@dataclass
class PersistStats:
    companies_created: int = 0
    companies_seen: int = 0
    jobs_created: int = 0
    jobs_updated: int = 0


def persist_jobs(
    jobs: list[Job],
    settings: Settings,
    *,
    profile_id: str = "default",
) -> tuple[dict[str, int], PersistStats]:
    """Upsert Job models into the `company` and `job` tables.

    Returns (url_to_job_id, stats). Safe to call repeatedly: existing rows are
    updated with fresh `last_seen` and `match_score`; description is refreshed.
    """
    url_to_id: dict[str, int] = {}
    stats = PersistStats()
    if not jobs:
        return url_to_id, stats

    now = clock.utcnow_iso()
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        company_ids: dict[str, int] = {}
        for job in jobs:
            if not job.domain:
                continue
            domain = job.domain.strip().lower()
            if domain not in company_ids:
                row = conn.execute(
                    "SELECT id FROM company WHERE domain=?", (domain,)
                ).fetchone()
                if row is None:
                    cur = conn.execute(
                        "INSERT INTO company(name, domain, source, created_at, profile_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (job.company, domain, job.source, now, profile_id),
                    )
                    company_ids[domain] = cast(int, cur.lastrowid)
                    stats.companies_created += 1
                else:
                    company_ids[domain] = cast(int, row["id"])
                    stats.companies_seen += 1
            company_id = company_ids[domain]

            existing = conn.execute(
                "SELECT id FROM job WHERE url=?", (job.url,)
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO job("
                    "company_id, title, url, location, employment_type, source, "
                    "match_score, first_seen, last_seen, profile_id, description"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        company_id,
                        job.title,
                        job.url,
                        job.location,
                        job.employment_type,
                        job.source,
                        job.match_score,
                        now,
                        now,
                        profile_id,
                        job.description or "",
                    ),
                )
                url_to_id[job.url] = cast(int, cur.lastrowid)
                stats.jobs_created += 1
            else:
                conn.execute(
                    "UPDATE job SET last_seen=?, match_score=?, title=?, description=? "
                    "WHERE id=?",
                    (
                        now,
                        job.match_score,
                        job.title,
                        job.description or "",
                        existing["id"],
                    ),
                )
                url_to_id[job.url] = cast(int, existing["id"])
                stats.jobs_updated += 1
        conn.commit()
    finally:
        conn.close()
    return url_to_id, stats