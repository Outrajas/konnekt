"""Deterministic offline reputation for companies.

Score components (max 1.0):
  +0.30  active hiring (>= 3 jobs in DB for this company)
  +0.10  some hiring (>= 1 job but < 3)
  +0.20  at least one job came from a known ATS vendor
  +0.20  at least one job publishes a salary
  +0.20  company has a positive invite_email_score (HN invite signal)
  +0.10  at least one substantive description (> 500 chars)

The `is_acceptable` gate implements the operator's rule:
  - If the job states a salary_min, accept regardless of reputation.
  - Otherwise, accept only if reputation_score >= 0.5.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from leadforge.config import Settings
from leadforge.db import connect, init_schema

_KNOWN_ATS = {"greenhouse", "lever", "ashby"}
_REPUTATION_THRESHOLD = 0.5


@dataclass
class ReputationResult:
    domain: str
    score: float
    signals: list[str]


def _score_from_conn(conn, domain: str) -> ReputationResult:
    domain = (domain or "").strip().lower()
    company_row = conn.execute(
        "SELECT * FROM company WHERE domain=?", (domain,)
    ).fetchone()
    if company_row is None:
        return ReputationResult(domain=domain, score=0.0, signals=["unknown-company"])

    jobs = conn.execute(
        "SELECT * FROM job WHERE company_id=?", (company_row["id"],)
    ).fetchall()

    score = 0.0
    signals: list[str] = []

    n_jobs = len(jobs)
    if n_jobs >= 3:
        score += 0.30
        signals.append(f"active-hiring:{n_jobs}-jobs")
    elif n_jobs >= 1:
        score += 0.10
        signals.append(f"hiring:{n_jobs}-job")

    if any((j["source"] or "") in _KNOWN_ATS for j in jobs):
        score += 0.20
        signals.append("known-ats")

    if any(j["salary_min"] is not None for j in jobs):
        score += 0.20
        signals.append("salary-transparent")

    invite_score = company_row["invite_email_score"] or 0
    if invite_score > 0:
        score += 0.20
        signals.append(f"hn-invite:{invite_score}")

    if any(len(j["description"] or "") > 500 for j in jobs):
        score += 0.10
        signals.append("detailed-posting")

    return ReputationResult(domain=domain, score=min(1.0, round(score, 2)), signals=signals)


def compute_company_reputation(domain: str, settings: Settings) -> ReputationResult:
    """Compute and store the reputation for every job under this company."""
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        result = _score_from_conn(conn, domain)
        if result.signals != ["unknown-company"]:
            conn.execute(
                "UPDATE job SET reputation_score=?, reputation_signals_json=? "
                "WHERE company_id=(SELECT id FROM company WHERE domain=?)",
                (result.score, json.dumps(result.signals), domain.strip().lower()),
            )
            conn.commit()
        return result
    finally:
        conn.close()


def is_acceptable(job_row, reputation: ReputationResult) -> tuple[bool, str]:
    """Apply the reputation gate. Returns (acceptable, reason)."""
    salary_min = job_row["salary_min"]
    if salary_min is not None:
        return True, f"salary:min={salary_min}"
    if reputation.score >= _REPUTATION_THRESHOLD:
        return True, f"reputation:{reputation.score:.2f}"
    return False, f"no-salary,rep={reputation.score:.2f}"