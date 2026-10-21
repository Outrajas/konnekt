"""Run all pending schema migrations on the live database. Idempotent.

    python scripts/migrate_job_intel.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))

from leadforge.db import connect, init_schema, schema_version  # noqa: E402


def main() -> int:
    db = Path("data/cache/leadforge.db")
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 1
    conn = connect(db)
    try:
        before = schema_version(conn)
        init_schema(conn)
        after = schema_version(conn)
        job_cols = [row[1] for row in conn.execute("PRAGMA table_info(job)").fetchall()]
        print(f"database: {db}")
        print(f"schema version: {before} -> {after}")
        print(f"job columns: {len(job_cols)}")
        for required in (
            "description", "salary_min", "salary_max", "salary_currency",
            "salary_period", "salary_source", "tech_stack_json",
            "reputation_score", "reputation_signals_json", "enriched_at",
        ):
            print(f"  job.{required}: {required in job_cols}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())