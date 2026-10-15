import sqlite3
from datetime import UTC, datetime, timedelta

from leadforge import clock


def _insert_id(conn: sqlite3.Connection, table: str, values: dict[str, object]) -> int:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
    )
    if cursor.lastrowid is None:
        raise RuntimeError(f"Insert into {table} did not return an id")
    return cursor.lastrowid


def upsert_company(
    conn: sqlite3.Connection,
    *,
    name: str,
    domain: str | None = None,
    source: str | None = None,
    invite_email_score: float = 0.0,
    created_at: str | None = None,
) -> int:
    values: dict[str, object] = {
        "name": name,
        "domain": domain,
        "source": source,
        "invite_email_score": invite_email_score,
        "created_at": created_at or clock.utcnow_iso(),
    }
    if domain is not None:
        conn.execute(
            """INSERT INTO company(name, domain, source, invite_email_score, created_at)
               VALUES (:name, :domain, :source, :invite_email_score, :created_at)
               ON CONFLICT(domain) DO UPDATE SET
                 name=excluded.name, source=excluded.source,
                 invite_email_score=excluded.invite_email_score""",
            values,
        )
        row = conn.execute("SELECT id FROM company WHERE domain=?", (domain,)).fetchone()
        assert row is not None
        return int(row["id"])
    return _insert_id(conn, "company", values)


def add_job(
    conn: sqlite3.Connection,
    *,
    title: str,
    source: str,
    url: str | None = None,
    company_id: int | None = None,
    location: str | None = None,
    employment_type: str | None = None,
    match_score: float | None = None,
    first_seen: str | None = None,
    last_seen: str | None = None,
) -> int:
    now = clock.utcnow_iso()
    values: dict[str, object] = {
        "company_id": company_id,
        "title": title,
        "url": url,
        "location": location,
        "employment_type": employment_type,
        "source": source,
        "match_score": match_score,
        "first_seen": first_seen or now,
        "last_seen": last_seen or now,
    }
    if url is not None:
        conn.execute(
            """INSERT INTO job(company_id, title, url, location, employment_type,
                               source, match_score, first_seen, last_seen)
               VALUES (:company_id, :title, :url, :location, :employment_type,
                       :source, :match_score, :first_seen, :last_seen)
               ON CONFLICT(url) DO UPDATE SET company_id=excluded.company_id,
                 title=excluded.title, location=excluded.location,
                 employment_type=excluded.employment_type, source=excluded.source,
                 match_score=excluded.match_score, last_seen=excluded.last_seen""",
            values,
        )
        row = conn.execute("SELECT id FROM job WHERE url=?", (url,)).fetchone()
        assert row is not None
        return int(row["id"])
    return _insert_id(conn, "job", values)


def add_person(
    conn: sqlite3.Connection,
    *,
    company_id: int | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    role_class: str | None = None,
    status: str = "new",
    stop_reason: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> int:
    now = clock.utcnow_iso()
    return _insert_id(
        conn,
        "person",
        {
            "company_id": company_id,
            "first_name": first_name,
            "last_name": last_name,
            "title": title,
            "role_class": role_class,
            "status": status,
            "stop_reason": stop_reason,
            "created_at": created_at or now,
            "updated_at": updated_at or now,
        },
    )


def add_page(
    conn: sqlite3.Connection,
    *,
    url: str,
    url_hash: str,
    host: str,
    tier: int,
    expires_at: str,
    status_code: int | None = None,
    blocked: bool = False,
    fetched_at: str | None = None,
    body_path: str | None = None,
) -> int:
    values: dict[str, object] = {
        "url": url,
        "url_hash": url_hash,
        "host": host,
        "tier": tier,
        "status_code": status_code,
        "blocked": int(blocked),
        "fetched_at": fetched_at or clock.utcnow_iso(),
        "expires_at": expires_at,
        "body_path": body_path,
    }
    conn.execute(
        """INSERT INTO page(url, url_hash, host, tier, status_code, blocked,
                           fetched_at, expires_at, body_path)
           VALUES (:url, :url_hash, :host, :tier, :status_code, :blocked,
                   :fetched_at, :expires_at, :body_path)
           ON CONFLICT(url_hash) DO UPDATE SET url=excluded.url, host=excluded.host,
             tier=excluded.tier, status_code=excluded.status_code, blocked=excluded.blocked,
             fetched_at=excluded.fetched_at, expires_at=excluded.expires_at,
             body_path=excluded.body_path""",
        values,
    )
    row = conn.execute("SELECT id FROM page WHERE url_hash=?", (url_hash,)).fetchone()
    assert row is not None
    return int(row["id"])


def add_evidence(
    conn: sqlite3.Connection,
    *,
    kind: str,
    value: str,
    professional: bool,
    person_id: int | None = None,
    company_id: int | None = None,
    page_id: int | None = None,
    created_at: str | None = None,
) -> int:
    return _insert_id(
        conn,
        "evidence",
        {
            "person_id": person_id,
            "company_id": company_id,
            "page_id": page_id,
            "kind": kind,
            "value": value,
            "professional": int(professional),
            "created_at": created_at or clock.utcnow_iso(),
        },
    )


def add_candidate(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    email: str,
    source: str,
    evidence_tier: str,
    pattern: str | None = None,
    found_on_web: bool = False,
    catch_all_domain: bool = False,
    verification_method: str | None = None,
    verification_verdict: str | None = None,
    confidence: float = 0.0,
    notes: str | None = None,
    created_at: str | None = None,
) -> int:
    conn.execute(
        """INSERT INTO email_candidate(person_id, email, pattern, source, evidence_tier,
               found_on_web, catch_all_domain, verification_method, verification_verdict,
               confidence, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(person_id, email) DO UPDATE SET pattern=excluded.pattern,
             source=excluded.source, evidence_tier=excluded.evidence_tier,
             found_on_web=excluded.found_on_web, catch_all_domain=excluded.catch_all_domain,
             verification_method=excluded.verification_method,
             verification_verdict=excluded.verification_verdict,
             confidence=excluded.confidence, notes=excluded.notes""",
        (
            person_id,
            email,
            pattern,
            source,
            evidence_tier,
            int(found_on_web),
            int(catch_all_domain),
            verification_method,
            verification_verdict,
            confidence,
            notes,
            created_at or clock.utcnow_iso(),
        ),
    )
    row = conn.execute(
        "SELECT id FROM email_candidate WHERE person_id=? AND email=?", (person_id, email)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def get_domain_pattern(conn: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM domain_pattern WHERE domain=?", (domain,)).fetchone()


def set_domain_pattern(
    conn: sqlite3.Connection,
    *,
    domain: str,
    pattern: str,
    sample_count: int,
    catch_all: bool | None = None,
    last_checked: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO domain_pattern(domain, pattern, sample_count, catch_all, last_checked)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(domain) DO UPDATE SET pattern=excluded.pattern,
             sample_count=excluded.sample_count, catch_all=excluded.catch_all,
             last_checked=excluded.last_checked""",
        (
            domain,
            pattern,
            sample_count,
            None if catch_all is None else int(catch_all),
            last_checked or clock.utcnow_iso(),
        ),
    )


def get_domain_state(conn: sqlite3.Connection, host: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM domain_state WHERE host=?", (host,)).fetchone()


def bump_domain_count(conn: sqlite3.Connection, host: str, *, now: str | None = None) -> int:
    timestamp = now or clock.utcnow_iso()
    day = datetime.fromisoformat(timestamp).astimezone(UTC).date().isoformat()
    conn.execute(
        """INSERT INTO domain_state(host, day, count_today, last_request_at)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(host) DO UPDATE SET
             count_today=CASE WHEN domain_state.day=excluded.day
                              THEN domain_state.count_today + 1 ELSE 1 END,
             day=excluded.day, last_request_at=excluded.last_request_at""",
        (host, day, timestamp),
    )
    row = get_domain_state(conn, host)
    assert row is not None
    return int(row["count_today"])


def pause_domain(
    conn: sqlite3.Connection,
    host: str,
    hours: int,
    reason: str,
    *,
    now: str | None = None,
) -> str:
    timestamp = now or clock.utcnow_iso()
    paused_until = (datetime.fromisoformat(timestamp) + timedelta(hours=hours)).isoformat()
    conn.execute(
        """INSERT INTO domain_state(host, paused_until, pause_reason)
           VALUES (?, ?, ?)
           ON CONFLICT(host) DO UPDATE SET paused_until=excluded.paused_until,
             pause_reason=excluded.pause_reason""",
        (host, paused_until, reason),
    )
    return paused_until
