"""End-to-end smoke test of persist + enrich + reputation on a temp DB."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))

from leadforge.config import Settings  # noqa: E402
from leadforge.job_enrich import enrich_job_row  # noqa: E402
from leadforge.job_persist import persist_jobs  # noqa: E402
from leadforge.job_reputation import (  # noqa: E402
    compute_company_reputation,
)
from leadforge.models import Job  # noqa: E402


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        settings = Settings(data_dir=td_path)
        try:
            settings = settings.model_copy(update={"db_path": td_path / "cache" / "leadforge.db"})
        except Exception:
            pass

        jobs = [
            Job(
                company="Acme", domain="acme.test",
                title="ML Engineer", url="https://acme.test/j/1",
                location="Remote", employment_type="full-time",
                description="We use Python, PyTorch, AWS. Salary: $120k-$180k per year.",
                source="greenhouse", match_score=0.8,
            ),
            Job(
                company="Acme", domain="acme.test",
                title="Backend Engineer", url="https://acme.test/j/2",
                location="Remote", employment_type="full-time",
                description="Python, SQL.",
                source="greenhouse", match_score=0.7,
            ),
        ]
        try:
            url_map, stats = persist_jobs(jobs, settings)
        except Exception as exc:
            failures.append(f"persist_jobs failed: {type(exc).__name__}: {exc}")
            url_map, stats = {}, None

        if stats and stats.jobs_created != 2:
            failures.append(f"expected 2 jobs created, got {stats.jobs_created}")

        for _url, jid in (url_map or {}).items():
            try:
                result = enrich_job_row(jid, settings)
            except Exception as exc:
                failures.append(f"enrich_job_row({jid}) failed: {exc}")
                continue
            if result is None:
                failures.append(f"enrich_job_row({jid}) returned None")

        try:
            rep = compute_company_reputation("acme.test", settings)
        except Exception as exc:
            failures.append(f"compute_company_reputation failed: {exc}")
            rep = None

        if rep is not None:
            if rep.score <= 0:
                failures.append(
                    "reputation score should be >0 "
                    f"for active+ATS+salary co, got {rep.score}"
                )
            if "known-ats" not in rep.signals:
                failures.append(f"missing known-ats signal: {rep.signals}")

        # Verify salary was actually extracted
        from leadforge.db import connect
        conn = connect(settings.db_path)
        try:
            row = conn.execute("SELECT * FROM job WHERE url='https://acme.test/j/1'").fetchone()
            if row is None:
                failures.append("job row missing after persist")
            else:
                if row["salary_min"] != 120_000:
                    failures.append(f"salary_min expected 120000, got {row['salary_min']}")
                if row["salary_source"] != "regex":
                    failures.append(f"salary_source expected regex, got {row['salary_source']}")
                if row["enriched_at"] is None:
                    failures.append("enriched_at should be set")
                if row["reputation_score"] is None:
                    failures.append("reputation_score should be set on job row")
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