"""Add job intelligence columns to the live database. Idempotent.

    python scripts/migrate_job_intel.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_NEW_COLUMNS: dict[str, str] = {
    "description": "TEXT",
    "salary_min": "INTEGER",
    "salary_max": "INTEGER",
    "salary_currency": "TEXT",
    "salary_period": "TEXT",
    "salary_source": "TEXT",
    "tech_stack_json": "TEXT",
    "reputation_score": "REAL",
    "reputation_signals_json": "TEXT",
    "enriched_at": "TEXT",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    cols = [row[1] for row in conn.execute("PRAGMA table_info(job)").fetchall()]
    for name, typ in _NEW_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE job ADD COLUMN {name} {typ}")
    conn.commit()


def main() -> int:
    db = Path("data/cache/leadforge.db")
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db)
    try:
        ensure_schema(conn)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(job)").fetchall()]
        print(f"database: {db}")
        print(f"job columns: {len(cols)}")
        for name in _NEW_COLUMNS:
            print(f"  job.{name}: {name in cols}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())