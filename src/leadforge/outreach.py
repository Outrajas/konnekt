"""Offline outreach logging, ramp limits, and suppression checks."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

OUTCOMES = ("none", "reply", "bounce", "auto_reply", "opt_out")


def daily_cap(conn: sqlite3.Connection, today: date | None = None) -> int:
    """Ramp the daily send allowance from the first recorded outreach date."""
    today = today or datetime.now(UTC).date()
    row = conn.execute("SELECT MIN(date(sent_at)) AS first_day FROM outreach").fetchone()
    if not row or row["first_day"] is None:
        return 5
    first_day = date.fromisoformat(str(row["first_day"]))
    age = max(0, (today - first_day).days)
    if age < 7:
        return 5
    if age < 14:
        return 10
    return 20


def sent_today(conn: sqlite3.Connection, today: date | None = None) -> int:
    today = today or datetime.now(UTC).date()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outreach WHERE date(sent_at)=?", (today.isoformat(),)
    ).fetchone()
    return int(row["n"])


def is_suppressed_or_contacted(
    conn: sqlite3.Connection,
    email: str,
    *,
    person_id: int | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    domain: str | None = None,
    company: str | None = None,
) -> bool:
    normalized = email.strip().casefold()
    email_domain = normalized.rsplit("@", 1)[-1]
    suppressed = conn.execute(
        "SELECT 1 FROM suppression WHERE (kind='email' AND value=?) "
        "OR (kind='domain' AND value=?) LIMIT 1",
        (normalized, email_domain),
    ).fetchone()
    if suppressed:
        return True
    if person_id is not None:
        return bool(
            conn.execute(
                "SELECT 1 FROM outreach WHERE person_id=? LIMIT 1", (person_id,)
            ).fetchone()
        )
    if (
        first_name is not None
        and last_name is not None
        and (domain is not None or company is not None)
    ):
        company_filter = (
            "lower(c.domain)=lower(?)" if domain is not None else "lower(c.name)=lower(?)"
        )
        return bool(
            conn.execute(
                "SELECT 1 FROM outreach o JOIN person p ON p.id=o.person_id "
                "JOIN company c ON c.id=p.company_id "
                "WHERE lower(p.first_name)=lower(?) AND lower(p.last_name)=lower(?) "
                f"AND {company_filter} LIMIT 1",
                (first_name, last_name, domain if domain is not None else company),
            ).fetchone()
        )
    return bool(
        conn.execute(
            "SELECT 1 FROM outreach WHERE lower(email)=? LIMIT 1", (normalized,)
        ).fetchone()
    )
