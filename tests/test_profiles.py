"""Tests for the multi-profile store."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from leadforge.profiles import (
    Profile,
    delete_profile,
    ensure_schema,
    get_profile,
    list_profiles,
    profile_to_json_file,
    resolve_profile_arg,
    resume_sha256,
    upsert_profile,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    yield c
    c.close()


def test_ensure_schema_creates_profiles_table(conn: sqlite3.Connection) -> None:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "profiles" in tables


def test_ensure_schema_is_idempotent(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    ensure_schema(conn)


def test_upsert_then_get(conn: sqlite3.Connection) -> None:
    p = Profile(
        profile_id="ai_ml",
        track="ai_ml",
        skills=["python", "pytorch"],
        target_titles=["ML Engineer", "AI Engineer"],
        localities=["Bangalore", "Remote"],
        seniority="mid",
        years_experience=3,
    )
    upsert_profile(conn, p)
    got = get_profile(conn, "ai_ml")
    assert got is not None
    assert got.profile_id == "ai_ml"
    assert got.track == "ai_ml"
    assert got.skills == ["python", "pytorch"]
    assert got.target_titles == ["ML Engineer", "AI Engineer"]
    assert got.years_experience == 3


def test_upsert_updates_existing(conn: sqlite3.Connection) -> None:
    upsert_profile(conn, Profile(profile_id="swe", track="swe", skills=["python"]))
    upsert_profile(conn, Profile(profile_id="swe", track="swe", skills=["python", "go"]))
    got = get_profile(conn, "swe")
    assert got is not None
    assert got.skills == ["python", "go"]
    # created_at preserved, updated_at changed (both set; we just check non-empty)
    assert got.created_at
    assert got.updated_at


def test_list_profiles_orders_by_track(conn: sqlite3.Connection) -> None:
    upsert_profile(conn, Profile(profile_id="b", track="swe"))
    upsert_profile(conn, Profile(profile_id="a", track="ai_ml"))
    profiles = list_profiles(conn)
    assert [p.track for p in profiles] == ["ai_ml", "swe"]


def test_delete_profile(conn: sqlite3.Connection) -> None:
    upsert_profile(conn, Profile(profile_id="temp", track="ai_ml"))
    assert delete_profile(conn, "temp") is True
    assert delete_profile(conn, "temp") is False
    assert get_profile(conn, "temp") is None


def test_resolve_by_id(conn: sqlite3.Connection) -> None:
    upsert_profile(conn, Profile(profile_id="ai_ml", track="ai_ml"))
    assert resolve_profile_arg(conn, "ai_ml") == "ai_ml"


def test_resolve_by_track_alias(conn: sqlite3.Connection) -> None:
    upsert_profile(conn, Profile(profile_id="my_ai", track="ai_ml"))
    assert resolve_profile_arg(conn, "ai_ml") == "my_ai"


def test_resolve_unknown_raises(conn: sqlite3.Connection) -> None:
    upsert_profile(conn, Profile(profile_id="a", track="swe"))
    with pytest.raises(ValueError):
        resolve_profile_arg(conn, "does_not_exist")


def test_resolve_none_single(conn: sqlite3.Connection) -> None:
    upsert_profile(conn, Profile(profile_id="only", track="swe"))
    assert resolve_profile_arg(conn, None) == "only"


def test_resolve_none_multiple(conn: sqlite3.Connection) -> None:
    upsert_profile(conn, Profile(profile_id="a", track="swe"))
    upsert_profile(conn, Profile(profile_id="b", track="ai_ml"))
    assert resolve_profile_arg(conn, None) == "default"


def test_resolve_none_empty(conn: sqlite3.Connection) -> None:
    assert resolve_profile_arg(conn, None) == "default"


def test_profile_to_json_file(tmp_path: Path) -> None:
    p = Profile(profile_id="x", track="swe", skills=["python"])
    out = tmp_path / "profile.json"
    profile_to_json_file(p, out)
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["profile_id"] == "x"
    assert data["skills"] == ["python"]


def test_resume_sha256(tmp_path: Path) -> None:
    p = tmp_path / "r.bin"
    p.write_bytes(b"hello world")
    h = resume_sha256(p)
    assert len(h) == 64
    assert h == resume_sha256(p)