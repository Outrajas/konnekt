"""Verify profiles schema + module. Used by check_pipeline.ps1 section 10.

Output contract (exactly one line per outcome class):
  SKIP: <reason>          -- no database; nothing to verify
  OK                      -- schema present and profiles module imports
  FAIL: <reason>          -- one line per failure
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))

from leadforge.profiles import DEPENDENT_TABLES, list_profiles  # noqa: E402


def main() -> int:
    db_path = Path("data/cache/leadforge.db")
    if not db_path.exists():
        print("SKIP: no database (run 'leadforge init-db')")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    failures: list[str] = []

    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    if "profiles" not in tables:
        failures.append("profiles table missing (run scripts/migrate_profiles.py)")
    else:
        for t in DEPENDENT_TABLES:
            if t not in tables:
                continue
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            if "profile_id" not in cols:
                failures.append(f"{t}.profile_id missing (run scripts/migrate_profiles.py)")

    try:
        list_profiles(conn)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"profiles module error: {exc}")

    conn.close()

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())