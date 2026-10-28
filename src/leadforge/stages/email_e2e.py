"""End-to-end email discovery for persons in the DB.

For each person whose company has a real (non-.wellfound) domain:
1. Build Contact from person + company
2. Generate email candidates via patterns (learned ranking if available)
3. Verify via SMTP RCPT TO (non-sending) - optional for dry-runs
4. Score + determine is_sendable
5. Persist to email_candidate table (idempotent per person_id+email)

Idempotent: running twice upserts by (person_id, email).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.models import Contact, EmailCandidate
from leadforge.scoring import evidence_tier, is_sendable, score
from leadforge.tools.email_patterns import generate, rank_patterns
from leadforge.tools.smtp_verify import verify_candidates_sync

log = logging.getLogger(__name__)


@dataclass
class PersonEmailResult:
    person_id: int
    name: str
    company: str
    domain: str
    candidates_generated: int = 0
    candidates_verified_send: int = 0
    candidates_sendable: int = 0
    best_email: str = ""
    best_confidence: float = 0.0
    persisted: int = 0
    note: str = ""


@dataclass
class EmailE2EResult:
    people: list[PersonEmailResult] = field(default_factory=list)
    total_candidates: int = 0
    total_sendable: int = 0
    total_persisted: int = 0


def _row_to_contact(row, domain: str, company: str) -> Contact:
    return Contact(
        first_name=row["first_name"] or "",
        last_name=row["last_name"] or "",
        domain=domain,
        company=company,
        title=row["title"] or "",
    )


def find_emails_for_person(
    person_row,
    *,
    settings: Settings,
    company: str,
    domain: str,
    smtp_enabled: bool = True,
    mx_resolver=None,
    probe_fn=None,
) -> list[EmailCandidate]:
    """Generate + optionally verify + score candidate emails for one person."""
    contact = _row_to_contact(person_row, domain, company)
    if not (contact.first_name or contact.last_name):
        return []

    try:
        ranked = rank_patterns(domain)
    except Exception:  # noqa: BLE001
        ranked = []

    candidates = generate(contact, patterns=ranked or None)
    if not candidates:
        return []

    if smtp_enabled:
        try:
            candidates = verify_candidates_sync(
                contact,
                candidates,
                settings=settings,
                mx_resolver=mx_resolver,
                probe_fn=probe_fn,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "SMTP verify failed for %s: %s: %s",
                contact.full_name, type(exc).__name__, exc,
            )

    pattern_set = set(ranked) if ranked else set()
    scored: list[EmailCandidate] = []
    for c in candidates:
        pm = bool(c.pattern and c.pattern in pattern_set)
        s = score(c, source_count=1, pattern_matches_domain=pm)
        tier = evidence_tier(c, pattern_matches_domain=pm)
        # evidence_tier returns Literal["A","B","C"] (already a str)
        tier_str = tier if isinstance(tier, str) else tier.value
        scored.append(
            c.model_copy(update={"confidence": s, "evidence_tier": tier_str})
        )
    return scored


def _enum_val(v):
    return v.value if hasattr(v, "value") else v


def _persist_email(
    conn: sqlite3.Connection,
    person_id: int,
    candidate: EmailCandidate,
) -> bool:
    now = clock.utcnow_iso()
    existing = conn.execute(
        "SELECT id FROM email_candidate WHERE person_id=? AND email=?",
        (person_id, candidate.email),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE email_candidate SET "
            "verification_method=?, verification_verdict=?, confidence=?, "
            "evidence_tier=?, catch_all_domain=?, notes=? WHERE id=?",
            (
                _enum_val(candidate.verification_method),
                _enum_val(candidate.verification_verdict),
                candidate.confidence,
                candidate.evidence_tier,
                1 if candidate.catch_all_domain else 0,
                candidate.notes or "",
                int(existing["id"]),
            ),
        )
        return False
    conn.execute(
        "INSERT INTO email_candidate "
        "(person_id, email, pattern, source, evidence_tier, found_on_web, "
        "catch_all_domain, verification_method, verification_verdict, "
        "confidence, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            person_id,
            candidate.email,
            candidate.pattern or "",
            candidate.source,
            candidate.evidence_tier,
            1 if candidate.found_on_web else 0,
            1 if candidate.catch_all_domain else 0,
            _enum_val(candidate.verification_method),
            _enum_val(candidate.verification_verdict),
            candidate.confidence,
            candidate.notes or "",
            now,
        ),
    )
    return True


def run_email_e2e(
    settings: Settings,
    *,
    profile_id: str = "ai_ml",
    max_companies: int | None = 3,
    max_people_per_company: int = 10,
    smtp_enabled: bool = True,
    dry_run: bool = False,
    mx_resolver=None,
    probe_fn=None,
) -> EmailE2EResult:
    """Orchestrate email discovery across persons with real domains."""
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        rows = conn.execute(
            "SELECT p.*, c.name AS company_name, c.domain AS company_domain "
            "FROM person p JOIN company c ON c.id=p.company_id "
            "WHERE c.domain IS NOT NULL AND c.domain != '' "
            "AND c.domain NOT LIKE '%.wellfound' "
            "ORDER BY c.name, p.id"
        ).fetchall()
    finally:
        conn.close()

    by_company: dict[str, list] = {}
    for row in rows:
        by_company.setdefault(row["company_domain"], []).append(row)

    keys = list(by_company.keys())
    if max_companies is not None:
        keys = keys[:max_companies]

    result = EmailE2EResult()

    for domain in keys:
        company_rows = by_company[domain][:max_people_per_company]
        company_name = company_rows[0]["company_name"] if company_rows else ""

        for prow in company_rows:
            full_name = f"{prow['first_name']} {prow['last_name']}".strip()
            try:
                candidates = find_emails_for_person(
                    prow,
                    settings=settings,
                    company=company_name,
                    domain=domain,
                    smtp_enabled=smtp_enabled and not dry_run,
                    mx_resolver=mx_resolver,
                    probe_fn=probe_fn,
                )
            except Exception as exc:  # noqa: BLE001
                result.people.append(
                    PersonEmailResult(
                        person_id=int(prow["id"]),
                        name=full_name, company=company_name, domain=domain,
                        note=f"error: {type(exc).__name__}: {exc}",
                    )
                )
                continue

            verified_send = sum(
                1 for c in candidates
                if _enum_val(c.verification_verdict) == "send"
            )
            sendable = [c for c in candidates if is_sendable(c, settings)]
            best = max(candidates, key=lambda c: c.confidence) if candidates else None

            persisted = 0
            if candidates and not dry_run:
                wconn = connect(settings.db_path)
                try:
                    for c in candidates:
                        if _persist_email(wconn, int(prow["id"]), c):
                            persisted += 1
                    wconn.commit()
                finally:
                    wconn.close()

            result.people.append(
                PersonEmailResult(
                    person_id=int(prow["id"]),
                    name=full_name, company=company_name, domain=domain,
                    candidates_generated=len(candidates),
                    candidates_verified_send=verified_send,
                    candidates_sendable=len(sendable),
                    best_email=best.email if best else "",
                    best_confidence=best.confidence if best else 0.0,
                    persisted=persisted,
                )
            )
            result.total_candidates += len(candidates)
            result.total_sendable += len(sendable)
            result.total_persisted += persisted

    return result