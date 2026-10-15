import sqlite3
from pathlib import Path

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS company (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        domain TEXT UNIQUE,
        source TEXT,
        invite_email_score REAL DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS job (
        id INTEGER PRIMARY KEY,
        company_id INTEGER REFERENCES company(id),
        title TEXT NOT NULL,
        url TEXT UNIQUE,
        location TEXT,
        employment_type TEXT,
        source TEXT NOT NULL,
        match_score REAL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS person (
        id INTEGER PRIMARY KEY,
        company_id INTEGER REFERENCES company(id),
        first_name TEXT,
        last_name TEXT,
        title TEXT,
        role_class TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        stop_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS page (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL,
        url_hash TEXT NOT NULL UNIQUE,
        host TEXT NOT NULL,
        tier INTEGER NOT NULL,
        status_code INTEGER,
        blocked INTEGER NOT NULL DEFAULT 0,
        fetched_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        body_path TEXT
    );

    CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY,
        person_id INTEGER REFERENCES person(id),
        company_id INTEGER REFERENCES company(id),
        page_id INTEGER REFERENCES page(id),
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        professional INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS email_candidate (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL REFERENCES person(id),
        email TEXT NOT NULL,
        pattern TEXT,
        source TEXT NOT NULL,
        evidence_tier TEXT NOT NULL,
        found_on_web INTEGER NOT NULL DEFAULT 0,
        catch_all_domain INTEGER NOT NULL DEFAULT 0,
        verification_method TEXT,
        verification_verdict TEXT,
        confidence REAL NOT NULL DEFAULT 0,
        notes TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(person_id, email)
    );

    CREATE TABLE IF NOT EXISTS domain_pattern (
        domain TEXT PRIMARY KEY,
        pattern TEXT NOT NULL,
        sample_count INTEGER NOT NULL,
        catch_all INTEGER,
        last_checked TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS domain_state (
        host TEXT PRIMARY KEY,
        paused_until TEXT,
        pause_reason TEXT,
        day TEXT,
        count_today INTEGER NOT NULL DEFAULT 0,
        last_request_at TEXT
    );

    CREATE TABLE IF NOT EXISTS run_log (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        stage TEXT NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_usage (
        day TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 0
    );
    """,
]


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite database with WAL and foreign-key enforcement enabled."""
    database_path = str(path)
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_version").fetchone()
    return int(row["version"])


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = schema_version(conn)
    for version, migration in enumerate(MIGRATIONS, start=1):
        if version > current:
            conn.executescript(migration)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
            conn.commit()
