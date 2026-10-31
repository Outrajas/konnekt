"""Batched LLM classification: 5 jobs per call.

Reduces per-run LLM cost by 5x and fits comfortably under Groq's TPD cap.
Schema: LLM returns a list of JobItemClassification with job_id correlation.
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class JobClassification(BaseModel):
    level: str = Field(
        description="One of: intern, entry, mid, senior, staff, lead, principal, exec, unknown",
    )
    india_eligible: bool = Field(
        description="True if India-based candidate can apply",
    )
    resume_match: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="1 sentence")


class BatchItem(BaseModel):
    job_id: int
    level: str
    india_eligible: bool
    resume_match: float = Field(ge=0.0, le=1.0)
    reasoning: str


class BatchResponse(BaseModel):
    classifications: list[BatchItem]


_SYSTEM = """You are classifying job postings for a job-seeking operator in India.
You will receive N jobs. Return EXACTLY N classifications, one per job_id.

LEVEL RULES (apply in order, stop at first match):
1. Title has "Intern" / "Internship" / "Trainee" -> intern
2. Title has "Junior" / "Jr." / "Entry" / "Fresher" / "Graduate" / "Associate" -> entry
3. Title has "Principal" / "Distinguished" / "Fellow" -> principal
4. Title has "Staff" -> staff
5. Title has "Lead" / "Tech Lead" / "Team Lead" -> lead
6. Title has "Head of" / "Director" / "VP" / "Chief" / "CTO" / "CEO" / "Founder" -> exec
7. Title has "Senior" / "Sr." / "III" / "IV" / "SDE 3+" -> senior
8. Description says "0-2 years" / "fresher" / "entry level" -> entry
9. Description says "2-5 years" / "3-5 years" / "2+ years" / "3+ years" -> mid
10. Description says "5+ years" / "6+ years" / "7+ years" -> senior
11. Description says "8+ years" / "10+ years" -> staff
12. OTHERWISE -> unknown

Do NOT default to "mid". "mid" requires explicit 2-5 years signal.
If nothing matches, return "unknown".

INDIA_ELIGIBLE:
- TRUE: onsite India, remote worldwide, remote Asia/India, unspecified remote
- FALSE: onsite US/EU/other, remote restricted to US/EU/other

RESUME_MATCH (0.0-1.0):
- Job skills overlap with operator skills / total job skills.
- No explicit skills in description -> 0.5

Return JSON: {"classifications": [{"job_id": <int>, ...}, ...]}

RULES:
- Return exactly one entry per input job_id.
- Preserve input job_id exactly.
- reasoning must be <= 120 characters.
"""


def _build_batch_prompt(batch: list[dict], skills: list[str], titles: list[str]) -> str:
    skills_str = ", ".join(skills[:15]) or "(none)"
    titles_str = ", ".join(titles[:5]) or "(none)"
    parts = [
        f"OPERATOR SKILLS: {skills_str}",
        f"OPERATOR TARGET TITLES: {titles_str}",
        "",
        "JOBS TO CLASSIFY:",
    ]
    for i, job in enumerate(batch, 1):
        desc = (job.get("description") or "")[:1200]
        parts.append(
            f"\n--- JOB {i} (job_id={job['id']}) ---\n"
            f"Title: {job.get('title', '?')}\n"
            f"Company: {job.get('company') or '?'}\n"
            f"Location: {job.get('location') or 'unspecified'}\n"
            f"Source: {job.get('source') or '?'}\n"
            f"Description: {desc}\n"
        )
    parts.append(
        "\nReturn a classification for EACH job, with the correct job_id in each object."
    )
    return "\n".join(parts)


async def classify_batch(
    batch: list[dict],
    skills: list[str],
    titles: list[str],
    *,
    llm_client=None,
) -> list[BatchItem]:
    from leadforge.llm import chat_json

    user = _build_batch_prompt(batch, skills, titles)

    if llm_client is not None:
        resp = llm_client.chat_json(_SYSTEM, user, schema=BatchResponse)
        if hasattr(resp, "__await__"):
            resp = await resp
        result = resp if isinstance(resp, BatchResponse) else BatchResponse(**resp)
        return result.classifications

    resp = chat_json(_SYSTEM, user, schema=BatchResponse)
    if hasattr(resp, "__await__"):
        resp = await resp
    return resp.classifications


async def enrich_jobs_with_llm(
    settings,
    *,
    profile_id: str = "ai_ml",
    job_ids: list[int] | None = None,
    max_jobs: int = 200,
    batch_size: int = 5,
    llm_client=None,
) -> dict:
    """Classify jobs in batches. Only unknown + not-yet-classified jobs."""

    from leadforge import clock
    from leadforge.db import connect, init_schema

    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        prof = conn.execute(
            "SELECT skills_json, target_titles_json FROM profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        skills = json.loads(prof["skills_json"] or "[]") if prof else []
        titles = json.loads(prof["target_titles_json"] or "[]") if prof else []

        if job_ids:
            placeholders = ",".join("?" * len(job_ids))
            rows = conn.execute(
                f"SELECT id, title, location, description, source, "
                f"(SELECT name FROM company WHERE id=company_id) AS company "
                f"FROM job WHERE id IN ({placeholders}) LIMIT ?",
                (*job_ids, max_jobs),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, location, description, source, "
                "(SELECT name FROM company WHERE id=company_id) AS company "
                "FROM job WHERE profile_id=? "
                "AND seniority='unknown' "
                "AND llm_classified_at IS NULL "
                "LIMIT ?",
                (profile_id, max_jobs),
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"classified": 0, "errors": 0, "batches": 0}

    job_dicts = [dict(r) for r in rows]
    stats = {"classified": 0, "errors": 0, "batches": 0}

    now = clock.utcnow_iso()
    for i in range(0, len(job_dicts), batch_size):
        batch = job_dicts[i : i + batch_size]
        try:
            classifications = await classify_batch(
                batch, skills, titles, llm_client=llm_client
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("batch classify failed: %s", exc)
            stats["errors"] += len(batch)
            continue

        stats["batches"] += 1
        returned_ids = {c.job_id for c in classifications}

        # Persist each classification
        conn = connect(settings.db_path)
        try:
            for item in classifications:
                conn.execute(
                    "UPDATE job SET seniority=?, india_eligible=?, "
                    "resume_match_score=?, intel_reasoning=?, llm_classified_at=? "
                    "WHERE id=?",
                    (
                        item.level,
                        1 if item.india_eligible else 0,
                        item.resume_match,
                        item.reasoning,
                        now,
                        item.job_id,
                    ),
                )
                stats["classified"] += 1

            # Mark jobs the LLM skipped as classified so we don't retry
            for job in batch:
                if job["id"] not in returned_ids:
                    conn.execute(
                        "UPDATE job SET llm_classified_at=? WHERE id=?",
                        (now, job["id"]),
                    )
            conn.commit()
        finally:
            conn.close()

    return stats