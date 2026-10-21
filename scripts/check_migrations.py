"""Verify migration framework: fresh DB runs all migrations, idempotent.

Output contract:
  OK          -- fresh DB has all expected tables/columns; second run is no-op
  FAIL: ...   -- one line per failure
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))

from leadforge.db import MIGRATIONS, connect, init_schema, schema_version  # noqa: E402

_REQUIRED_JOB_COLUMNS = (
    "id", "company_id", "title", "url", "location", "employment_type",
    "source", "match_score", "first_seen", "last_seen", "profile_id",
    "description", "salary_min", "salary_max", "salary_currency",
    "salary_period", "salary_source", "tech_stack_json",
    "reputation_score", "reputation_signals_json", "enriched_at",
)


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        fresh = Path(td) / "fresh.db"
        conn = connect(fresh)
        try:
            init_schema(conn)
            v1 = schema_version(conn)
            if v1 != len(MIGRATIONS):
                failures.append(f"fresh DB version {v1} != {len(MIGRATIONS)} migrations")

            job_cols = {row[1] for row in conn.execute("PRAGMA table_info(job)").fetchall()}
            missing = [c for c in _REQUIRED_JOB_COLUMNS if c not in job_cols]
            if missing:
                failures.append(f"fresh DB missing job columns: {missing}")

            company_cols = {row[1] for row in conn.execute("PRAGMA table_info(company)").fetchall()}
            if "profile_id" not in company_cols:
                failures.append("fresh DB missing company.profile_id")

            # Idempotency: run again, expect no error and no version change
            try:
                init_schema(conn)
                v2 = schema_version(conn)
                if v2 != v1:
                    failures.append(f"init_schema not idempotent: {v1} -> {v2}")
            except sqlite3.Error as exc:
                failures.append(f"init_schema second run failed: {exc}")
        finally:
            conn.close()

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print(f"OK ({len(MIGRATIONS)} migrations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())