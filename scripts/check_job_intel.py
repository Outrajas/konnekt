"""Verify job_intel module + schema. Used by check_pipeline section 15.

Output contract:
  SKIP: <reason>   -- no database; module still verified
  OK               -- module sane, schema present
  FAIL: <reason>   -- one line per failure
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))

from leadforge.job_intel import extract_salary, extract_tech_stack  # noqa: E402

_REQUIRED_COLUMNS = (
    "salary_min", "salary_max", "salary_currency", "salary_period",
    "salary_source", "tech_stack_json", "reputation_score",
    "reputation_signals_json", "enriched_at", "description",
)


def main() -> int:
    failures: list[str] = []

    try:
        s = extract_salary("$120,000 - $180,000 per year")
        if not s or s.currency != "USD" or s.min_amount != 120_000 or s.max_amount != 180_000:
            failures.append(f"extract_salary sanity failed: {s}")
        s2 = extract_salary("₹12-15 LPA")
        if not s2 or s2.currency != "INR" or s2.min_amount != 1_200_000:
            failures.append(f"extract_salary LPA sanity failed: {s2}")
        t = extract_tech_stack("We use Python, SQL, and AWS with PyTorch.")
        for skill in ("python", "sql", "aws", "pytorch"):
            if skill not in t:
                failures.append(f"extract_tech_stack missed {skill}: {t}")
                break
    except Exception as exc:
        failures.append(f"module import error: {type(exc).__name__}: {exc}")

    db = Path("data/cache/leadforge.db")
    if not db.exists():
        print("SKIP: no database (run 'leadforge init-db' then scripts/migrate_job_intel.py)")
        if failures:
            for f in failures:
                print(f"FAIL: {f}")
            return 1
        return 0

    conn = sqlite3.connect(db)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(job)").fetchall()]
        for name in _REQUIRED_COLUMNS:
            if name not in cols:
                failures.append(f"job.{name} missing (run scripts/migrate_job_intel.py)")
    finally:
        conn.close()

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())