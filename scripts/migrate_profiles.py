"""Add profiles table + profile_id columns to the live database.

Idempotent. Safe to run repeatedly.

    python scripts/migrate_profiles.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))

from leadforge.profiles import DEPENDENT_TABLES, ensure_schema  # noqa: E402


def main() -> int:
    db_path = Path("data/cache/leadforge.db")
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        print("run 'leadforge init-db' first", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        ensure_schema(conn)
    except sqlite3.Error as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        conn.close()
        return 1

    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"database: {db_path}")
    print(f"tables:   {len(tables)}")
    print(f"profiles: {'profiles' in tables}")
    for t in DEPENDENT_TABLES:
        if t in tables:
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            has = "profile_id" in cols
            print(f"  {t}.profile_id: {has}")
    count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    print(f"rows in profiles: {count}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())