"""Multi-track operator profiles.

Additive to the existing single-profile path (data/profile.json). Nothing in
this module modifies how OperatorProfile is loaded; it provides a parallel,
per-track store backed by a SQLite `profiles` table plus a `profile_id`
column on dependent tables for future filtering.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

VALID_TRACKS = ("data_analyst", "swe", "ai_ml", "default")
DEPENDENT_TABLES = ("company", "job", "person", "email_candidate", "outreach", "run_log")


@dataclass
class Profile:
    profile_id: str
    track: str
    resume_path: str | None = None
    resume_sha256: str | None = None
    skills: list[str] = field(default_factory=list)
    target_titles: list[str] = field(default_factory=list)
    localities: list[str] = field(default_factory=list)
    seniority: str = "mid"
    years_experience: int | None = None
    notes: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resume_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_to_profile(row: sqlite3.Row) -> Profile:
    return Profile(
        profile_id=row["profile_id"],
        track=row["track"],
        resume_path=row["resume_path"],
        resume_sha256=row["resume_sha256"],
        skills=json.loads(row["skills_json"] or "[]"),
        target_titles=json.loads(row["target_titles_json"] or "[]"),
        localities=json.loads(row["localities_json"] or "[]"),
        seniority=row["seniority"] or "mid",
        years_experience=row["years_experience"],
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create profiles table + profile_id columns if missing. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            profile_id         TEXT PRIMARY KEY,
            track              TEXT NOT NULL,
            resume_path        TEXT,
            resume_sha256      TEXT,
            skills_json        TEXT NOT NULL DEFAULT '[]',
            target_titles_json TEXT NOT NULL DEFAULT '[]',
            localities_json    TEXT NOT NULL DEFAULT '[]',
            seniority          TEXT NOT NULL DEFAULT 'mid',
            years_experience   INTEGER,
            notes              TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_track ON profiles(track)")

    for table in DEPENDENT_TABLES:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        except sqlite3.OperationalError:
            continue
        if not cols:
            continue
        if "profile_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN profile_id TEXT")
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_profile ON {table}(profile_id)"
            )
            conn.execute(f"UPDATE {table} SET profile_id='default' WHERE profile_id IS NULL")
    conn.commit()


def upsert_profile(conn: sqlite3.Connection, profile: Profile) -> Profile:
    ensure_schema(conn)
    now = _now()
    existing = conn.execute(
        "SELECT created_at FROM profiles WHERE profile_id=?", (profile.profile_id,)
    ).fetchone()
    created = existing["created_at"] if existing else now
    conn.execute(
        """
        INSERT INTO profiles (
            profile_id, track, resume_path, resume_sha256,
            skills_json, target_titles_json, localities_json,
            seniority, years_experience, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile_id) DO UPDATE SET
            track=excluded.track,
            resume_path=excluded.resume_path,
            resume_sha256=excluded.resume_sha256,
            skills_json=excluded.skills_json,
            target_titles_json=excluded.target_titles_json,
            localities_json=excluded.localities_json,
            seniority=excluded.seniority,
            years_experience=excluded.years_experience,
            notes=excluded.notes,
            updated_at=excluded.updated_at
        """,
        (
            profile.profile_id, profile.track, profile.resume_path, profile.resume_sha256,
            json.dumps(profile.skills), json.dumps(profile.target_titles),
            json.dumps(profile.localities), profile.seniority, profile.years_experience,
            profile.notes, created, now,
        ),
    )
    conn.commit()
    result = get_profile(conn, profile.profile_id)
    assert result is not None
    return result


def get_profile(conn: sqlite3.Connection, profile_id: str) -> Profile | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM profiles WHERE profile_id=?", (profile_id,)
    ).fetchone()
    return _row_to_profile(row) if row else None


def list_profiles(conn: sqlite3.Connection) -> list[Profile]:
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM profiles ORDER BY track, profile_id").fetchall()
    return [_row_to_profile(r) for r in rows]


def delete_profile(conn: sqlite3.Connection, profile_id: str) -> bool:
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM profiles WHERE profile_id=?", (profile_id,))
    conn.commit()
    return cur.rowcount > 0


def resolve_profile_arg(conn: sqlite3.Connection, arg: str | None) -> str:
    """Resolve a --profile argument to a concrete profile_id.

    Rules:
      - arg given & matches profile_id -> that id
      - arg given & matches exactly one track -> that id
      - arg given & matches nothing -> ValueError
      - arg None & 0 profiles exist -> "default" (legacy single-profile)
      - arg None & 1 profile exists -> that id
      - arg None & 2+ profiles exist -> "default" (explicit --profile required)
    """
    if arg:
        if get_profile(conn, arg):
            return arg
        rows = conn.execute(
            "SELECT profile_id FROM profiles WHERE track=?", (arg,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["profile_id"]
        if len(rows) > 1:
            raise ValueError(
                f"profile '{arg}' matches {len(rows)} tracks; use the profile_id"
            )
        raise ValueError(f"profile '{arg}' not found (tried profile_id and track)")

    rows = conn.execute("SELECT profile_id FROM profiles").fetchall()
    if len(rows) == 1:
        return rows[0]["profile_id"]
    return "default"


def profile_to_json_file(profile: Profile, path: Path) -> None:
    """Write a profile to a JSON file for the legacy single-profile path."""
    data = {
        "profile_id": profile.profile_id,
        "track": profile.track,
        "skills": profile.skills,
        "target_titles": profile.target_titles,
        "localities": profile.localities,
        "seniority": profile.seniority,
        "years_experience": profile.years_experience,
        "notes": profile.notes,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")