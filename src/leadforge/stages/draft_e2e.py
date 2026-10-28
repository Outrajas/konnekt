"""End-to-end draft generation for shortlisted (job, person, email) triples.

For each person with a sendable email and an associated job:
1. Load profile, job, person, best email candidate
2. Build system + user prompt with all provenanced facts
3. Call llm.chat_json(system, user, schema=DraftResponse) to get
   subject + body + evidence_used
4. Cross-check every "evidence_used" entry against the actual facts we gave
   the LLM (attribution: no hallucinated facts)
5. Write draft to data/output/drafts/<company>__<person>.md
6. Persist a row in the `draft` table (created via migration 7)

Dry-run generates a template draft (no LLM call), useful for testing.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.llm import LLMNotConfigured, LLMOutputError, chat_json

log = logging.getLogger(__name__)


# ---------------- schema for the LLM call ----------------


class DraftResponse(BaseModel):
    subject: str = Field(..., description="Email subject line, <= 80 chars")
    body: str = Field(..., description="Email body, 100-220 words, plain text")
    evidence_used: list[str] = Field(
        default_factory=list,
        description="Short labels of facts from the prompt actually referenced",
    )


# ---------------- result dataclasses ----------------


@dataclass
class PersonDraftResult:
    person_id: int
    name: str
    company: str
    domain: str
    email: str = ""
    job_title: str = ""
    subject: str = ""
    body_chars: int = 0
    evidence_used: int = 0
    hallucinated: list[str] = field(default_factory=list)
    quality_notes: str = ""
    draft_path: str = ""
    persisted: bool = False
    note: str = ""


@dataclass
class DraftE2EResult:
    people: list[PersonDraftResult] = field(default_factory=list)
    total_drafted: int = 0
    total_persisted: int = 0


# ---------------- prompts ----------------


_SYSTEM_PROMPT = """You are drafting a short, human, professional outreach email
from a job seeker to a specific person at a target company.

ABSOLUTE RULES:
- Never mention the recipient's personal life, family, location, or anything
  that is not clearly public professional work.
- Never invent facts. If a fact is not in the input, do not state it.
- Keep the body 100-220 words. Plain text, no markdown, no emojis.
- The subject must be <= 80 characters, specific, no clickbait.
- Refer to exactly ONE concrete fact about the person OR the job to prove you
  did homework.
- Tone: peer-to-peer, curious, not sycophantic. Do not beg for a job.

Return JSON matching the DraftResponse schema.
"""


def _build_user_prompt(
    person: dict,
    job: dict,
    company: str,
    profile: dict,
    evidence_facts: list[str],
) -> str:
    """Compose the user prompt from provenanced facts only."""
    skills = ", ".join(profile.get("skills", [])[:8]) or "(unspecified)"
    titles = ", ".join(profile.get("target_titles", [])[:4]) or "(unspecified)"
    locality = ", ".join(profile.get("localities", [])[:3]) or "(unspecified)"

    facts_block = "\n".join(f"- {f}" for f in evidence_facts) or "(none)"

    salary_line = ""
    if job.get("salary_min") is not None:
        cur = job.get("salary_currency") or ""
        lo = job.get("salary_min")
        hi = job.get("salary_max") or lo
        salary_line = f"\n- Salary band: {cur}{lo}-{hi}"

    tech_line = ""
    try:
        tech = json.loads(job.get("tech_stack_json") or "[]")
        if tech:
            tech_line = f"\n- Tech stack: {', '.join(tech[:8])}"
    except (json.JSONDecodeError, TypeError):
        pass

    return f"""RECIPIENT
- Name: {person.get("first_name", "")} {person.get("last_name", "")}
- Title: {person.get("title", "(unknown)")}

COMPANY
- Name: {company}

JOB
- Title: {job.get("title", "(unknown)")}
- Location: {job.get("location") or "(unspecified)"}{salary_line}{tech_line}

MY BACKGROUND
- Target titles: {titles}
- Skills: {skills}
- Locality: {locality}

PROVENANCED FACTS ABOUT THE RECIPIENT OR JOB
{facts_block}

Draft a personalized outreach email. Reference at most one fact from the
PROVENANCED FACTS list. In `evidence_used`, list the labels of the facts you
referenced, exactly as they appear above.
"""


def _build_evidence_facts(
    person: dict, job: dict, email_row
) -> list[str]:
    """Assemble the short fact list we hand to the LLM. Every item is
    something we can attribute back to a DB column, so the LLM has no
    reason to invent."""
    facts: list[str] = []
    title = (person.get("title") or "").strip()
    if title:
        facts.append(f"recipient_title: {title}")
    job_title = (job.get("title") or "").strip()
    if job_title:
        facts.append(f"open_role: {job_title}")
    loc = (job.get("location") or "").strip()
    if loc:
        facts.append(f"role_location: {loc}")
    try:
        tech = json.loads(job.get("tech_stack_json") or "[]")
        if tech:
            facts.append(f"role_tech: {', '.join(tech[:5])}")
    except (json.JSONDecodeError, TypeError):
        pass
    if job.get("salary_min") is not None:
        cur = job.get("salary_currency") or ""
        lo = job.get("salary_min")
        hi = job.get("salary_max") or lo
        facts.append(f"role_salary: {cur}{lo}-{hi}")
    return facts


def _verify_evidence(
    evidence_used: list[str], facts: list[str]
) -> list[str]:
    """Return hallucinated evidence labels."""
    known = set()
    for f in facts:
        known.add(f.split(":", 1)[0].strip())
    return [e for e in evidence_used if e.strip() not in known]


# ---------------- dry-run template ----------------


def _template_draft(person: dict, job: dict, company: str, profile: dict) -> DraftResponse:
    first = person.get("first_name") or "there"
    title = person.get("title") or "your team"
    job_title = job.get("title") or "an open role"
    skills = ", ".join(profile.get("skills", [])[:3]) or "my background"
    subject = f"Question about the {job_title} role at {company}"
    body = (
        f"Hi {first},\n\n"
        f"I came across the {job_title} opening at {company} and noticed your "
        f"work as {title}. The role lines up closely with my background in "
        f"{skills}.\n\n"
        f"Would you be open to a short conversation about what a strong first "
        f"90 days would look like on the team?\n\n"
        f"I can share a 3-line summary of my relevant experience if that's "
        f"useful — no pressure either way.\n\n"
        f"Thanks for reading,\n\n"
        f"(your name)"
    )
    return DraftResponse(subject=subject, body=body, evidence_used=[])


# ---------------- LLM invocation ----------------


async def _call_llm(
    system: str, user: str, *, llm_client=None
) -> DraftResponse:
    """Call chat_json via injected client or the real module."""
    if llm_client is not None:
        chat = getattr(llm_client, "chat_json", None)
        if chat is None:
            raise RuntimeError("llm_client has no chat_json method")
        resp = chat(system, user, schema=DraftResponse)
        if hasattr(resp, "__await__"):
            resp = await resp
        return resp if isinstance(resp, DraftResponse) else DraftResponse(**resp)

    resp = chat_json(system, user, schema=DraftResponse)
    if hasattr(resp, "__await__"):
        resp = await resp
    return resp


# ---------------- generation ----------------


async def generate_draft_for_person(
    person_row,
    email_row,
    job_row,
    profile: dict,
    company: str,
    *,
    llm_client=None,
    dry_run: bool = False,
) -> DraftResponse:
    """Generate a DraftResponse for one (person, email, job) triple."""
    person = dict(person_row) if not isinstance(person_row, dict) else person_row
    job = dict(job_row) if not isinstance(job_row, dict) else job_row

    if dry_run:
        return _template_draft(person, job, company, profile)

    facts = _build_evidence_facts(person, job, email_row)
    user = _build_user_prompt(person, job, company, profile, facts)
    result = await _call_llm(_SYSTEM_PROMPT, user, llm_client=llm_client)
    return result


# ---------------- persistence ----------------


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", value or "").strip("_").lower()
    return s or "unknown"


def _write_draft_file(
    settings: Settings,
    company: str,
    person_name: str,
    subject: str,
    body: str,
) -> Path:
    out_dir = settings.output_dir / "drafts"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_slugify(company)}__{_slugify(person_name)}.md"
    content = f"# {company} — {person_name}\n\n**Subject:** {subject}\n\n---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    return path


def _ensure_draft_table(conn) -> None:
    """Create the draft table if missing (defensive; migration 7 also handles it)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS draft (
            id INTEGER PRIMARY KEY,
            person_id INTEGER REFERENCES person(id),
            job_id INTEGER REFERENCES job(id),
            email_candidate_id INTEGER,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            evidence_used_json TEXT NOT NULL DEFAULT '[]',
            quality_notes TEXT,
            draft_path TEXT,
            created_at TEXT NOT NULL,
            profile_id TEXT
        )
        """
    )


def _persist_draft(
    settings: Settings,
    *,
    person_id: int,
    job_id: int | None,
    email_candidate_id: int | None,
    draft: DraftResponse,
    draft_path: Path,
    quality_notes: str,
    profile_id: str,
) -> bool:
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        from leadforge.profiles import ensure_schema as _ensure_ps
        _ensure_ps(conn)
        _ensure_draft_table(conn)
        now = clock.utcnow_iso()
        existing = conn.execute(
            "SELECT id FROM draft WHERE person_id=? AND job_id IS ?",
            (person_id, job_id),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE draft SET subject=?, body=?, evidence_used_json=?, "
                "quality_notes=?, draft_path=? WHERE id=?",
                (
                    draft.subject,
                    draft.body,
                    json.dumps(draft.evidence_used),
                    quality_notes,
                    str(draft_path),
                    int(existing["id"]),
                ),
            )
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO draft(person_id, job_id, email_candidate_id, subject, "
            "body, evidence_used_json, quality_notes, draft_path, created_at, "
            "profile_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                person_id,
                job_id,
                email_candidate_id,
                draft.subject,
                draft.body,
                json.dumps(draft.evidence_used),
                quality_notes,
                str(draft_path),
                now,
                profile_id,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# ---------------- orchestrator ----------------


async def run_draft_e2e(
    settings: Settings,
    *,
    profile_id: str = "ai_ml",
    max_people: int = 10,
    dry_run: bool = False,
    llm_client=None,
    fetcher=None,
) -> DraftE2EResult:
    """For each person with a sendable email + associated job, draft an email."""
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        from leadforge.profiles import ensure_schema as ensure_profiles_schema
        ensure_profiles_schema(conn)
        rows = conn.execute(
            "SELECT p.id AS person_id, p.first_name, p.last_name, p.title, "
            "       c.name AS company_name, c.domain AS company_domain, "
            "       j.id AS job_id, j.title AS job_title, j.location AS job_location, "
            "       j.salary_min, j.salary_max, j.salary_currency, j.tech_stack_json, "
            "       ec.id AS email_candidate_id, ec.email, ec.confidence "
            "FROM person p "
            "JOIN company c ON c.id = p.company_id "
            "LEFT JOIN email_candidate ec ON ec.person_id = p.id "
            "LEFT JOIN job j ON j.company_id = c.id "
            "WHERE c.domain IS NOT NULL AND c.domain != '' "
            "  AND c.domain NOT LIKE '%.wellfound' "
            "  AND (j.profile_id = ? OR j.profile_id IS NULL) "
            "ORDER BY ec.confidence DESC, p.id"
        , (profile_id,)).fetchall()

        # Load profile row
        prof_row = conn.execute(
            "SELECT * FROM profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()
    finally:
        conn.close()

    profile = {}
    if prof_row is not None:
        profile = {
            "skills": json.loads(prof_row["skills_json"] or "[]"),
            "target_titles": json.loads(prof_row["target_titles_json"] or "[]"),
            "localities": json.loads(prof_row["localities_json"] or "[]"),
        }

    # Pick best email per person (highest confidence, only sendable)
    best_email_by_person: dict[int, dict] = {}
    person_job: dict[int, dict] = {}
    for row in rows:
        pid = int(row["person_id"])
        if pid not in person_job and row["job_id"] is not None:
            person_job[pid] = dict(row)
        email = row["email"]
        if not email:
            continue
        existing = best_email_by_person.get(pid)
        if existing is None or (row["confidence"] or 0) > (existing["confidence"] or 0):
            best_email_by_person[pid] = dict(row)

    result = DraftE2EResult()
    processed = 0
    for pid, job_row in list(person_job.items()):
        if processed >= max_people:
            break
        email_row = best_email_by_person.get(pid)
        if email_row is None:
            continue  # no email candidate for this person
        processed += 1

        name = f"{job_row['first_name']} {job_row['last_name']}".strip()
        company = job_row["company_name"] or "?"

        try:
            draft = await generate_draft_for_person(
                job_row, email_row, job_row, profile, company,
                llm_client=llm_client, dry_run=dry_run,
            )
        except LLMNotConfigured as exc:
            result.people.append(
                PersonDraftResult(
                    person_id=pid, name=name, company=company,
                    domain=job_row["company_domain"],
                    note=f"llm_not_configured: {exc}",
                )
            )
            continue
        except LLMOutputError as exc:
            result.people.append(
                PersonDraftResult(
                    person_id=pid, name=name, company=company,
                    domain=job_row["company_domain"],
                    note=f"llm_output_error: {exc}",
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001
            result.people.append(
                PersonDraftResult(
                    person_id=pid, name=name, company=company,
                    domain=job_row["company_domain"],
                    note=f"error: {type(exc).__name__}: {exc}",
                )
            )
            continue

        # Attribution: check for hallucinated evidence labels
        facts = _build_evidence_facts(job_row, job_row, email_row)
        hallucinated = _verify_evidence(draft.evidence_used, facts)

        # Quality check via existing module if available
        quality_notes = ""
        try:
            from leadforge.draft_quality import quality_issues
            issues = quality_issues(draft.body)
            quality_notes = "; ".join(str(i) for i in issues) if issues else ""
        except Exception:  # noqa: BLE001
            pass

        draft_path = None
        persisted = False
        if not dry_run:
            draft_path = _write_draft_file(settings, company, name, draft.subject, draft.body)
            job_id_val = int(job_row["job_id"]) if job_row.get("job_id") else None
            ec_id_val = (
                int(email_row["email_candidate_id"])
                if email_row.get("email_candidate_id")
                else None
            )
            persisted = _persist_draft(
                settings,
                person_id=pid,
                job_id=job_id_val,
                email_candidate_id=ec_id_val,
                draft=draft,
                draft_path=draft_path,
                quality_notes=quality_notes,
                profile_id=profile_id,
            )

        result.people.append(
            PersonDraftResult(
                person_id=pid, name=name, company=company,
                domain=job_row["company_domain"],
                email=email_row["email"] or "",
                job_title=job_row.get("job_title") or "",
                subject=draft.subject,
                body_chars=len(draft.body),
                evidence_used=len(draft.evidence_used),
                hallucinated=hallucinated,
                quality_notes=quality_notes,
                draft_path=str(draft_path) if draft_path else "",
                persisted=persisted,
            )
        )
        result.total_drafted += 1
        result.total_persisted += 1 if persisted else 0

    return result