"""Learn the majority email pattern from Tier-A evidence only."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from leadforge.models import Contact, EmailCandidate
from leadforge.tools.email_patterns import infer_pattern


def record_pattern_sample(
    conn: sqlite3.Connection,
    contact: Contact,
    candidate: EmailCandidate,
    *,
    now: datetime | None = None,
) -> bool:
    """Record one unique Tier-A name/address sample and refresh the domain majority."""
    if candidate.evidence_tier != "A":
        return False
    pattern = infer_pattern(contact, candidate.email)
    if pattern is None:
        return False
    domain = contact.domain.strip().lower().rstrip(".")
    email = candidate.email.strip().casefold()
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO domain_pattern_sample(domain,pattern,email,created_at)
           VALUES(?,?,?,?)""",
        (domain, pattern, email, timestamp),
    )
    if cursor.rowcount == 0:
        return False
    counts = conn.execute(
        """SELECT pattern,COUNT(*) AS n FROM domain_pattern_sample
           WHERE domain=? GROUP BY pattern ORDER BY n DESC,pattern""",
        (domain,),
    ).fetchall()
    maximum = max(int(row["n"]) for row in counts)
    tied = [row["pattern"] for row in counts if int(row["n"]) == maximum]
    prior = conn.execute("SELECT pattern FROM domain_pattern WHERE domain=?", (domain,)).fetchone()
    majority = prior["pattern"] if prior and prior["pattern"] in tied else tied[0]
    total = sum(int(row["n"]) for row in counts)
    conn.execute(
        """INSERT INTO domain_pattern(domain,pattern,sample_count,last_checked)
           VALUES(?,?,?,?)
           ON CONFLICT(domain) DO UPDATE SET pattern=excluded.pattern,
             sample_count=excluded.sample_count,last_checked=excluded.last_checked""",
        (domain, majority, total, timestamp),
    )
    conn.commit()
    return True


def is_established(conn: sqlite3.Connection, domain: str) -> bool:
    """An established pattern has at least two unique matching samples."""
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM domain_pattern_sample
           WHERE domain=? COLLATE NOCASE AND pattern=(
             SELECT pattern FROM domain_pattern WHERE domain=? COLLATE NOCASE
           )""",
        (domain.strip().lower().rstrip("."), domain.strip().lower().rstrip(".")),
    ).fetchone()
    return bool(row and int(row["n"]) >= 2)
