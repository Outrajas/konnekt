"""Enrich a persisted job with salary + tech stack. Idempotent per row."""
from __future__ import annotations

import json
from dataclasses import dataclass

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.job_intel import SalaryInfo, extract_salary, extract_tech_stack


@dataclass
class EnrichResult:
    job_id: int
    salary: SalaryInfo | None
    tech_stack: list[str]
    skipped: bool = False


def _row_salary(row) -> SalaryInfo | None:
    lo = row["salary_min"]
    hi = row["salary_max"]
    if lo is None or hi is None:
        return None
    return SalaryInfo(
        min_amount=int(lo),
        max_amount=int(hi),
        currency=row["salary_currency"] or "",
        period=row["salary_period"] or "",
        raw="",
    )


def _row_tech(row) -> list[str]:
    raw = row["tech_stack_json"]
    if not raw:
        return []
    try:
        return list(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return []


def enrich_job_row(job_id: int, settings: Settings) -> EnrichResult | None:
    """Read title + description from a job row, extract salary + tech, write back.

    Returns None if the job id does not exist. If `enriched_at` is already set,
    returns the stored result with `skipped=True` and makes no changes.
    """
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        row = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None

        if row["enriched_at"] is not None:
            return EnrichResult(
                job_id=job_id,
                salary=_row_salary(row),
                tech_stack=_row_tech(row),
                skipped=True,
            )

        title = row["title"] or ""
        description = row["description"] or ""
        salary = extract_salary(description) or extract_salary(title)
        tech = extract_tech_stack(description, title)

        now = clock.utcnow_iso()
        salary_source = "regex" if salary else "none"
        conn.execute(
            "UPDATE job SET "
            "salary_min=?, salary_max=?, salary_currency=?, salary_period=?, "
            "salary_source=?, tech_stack_json=?, enriched_at=? "
            "WHERE id=?",
            (
                salary.min_amount if salary else None,
                salary.max_amount if salary else None,
                salary.currency if salary else None,
                salary.period if salary else None,
                salary_source,
                json.dumps(tech),
                now,
                job_id,
            ),
        )
        conn.commit()
        return EnrichResult(
            job_id=job_id, salary=salary, tech_stack=tech, skipped=False
        )
    finally:
        conn.close()