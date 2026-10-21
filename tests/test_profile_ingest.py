"""Tests for profile ingest --track persistence."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leadforge import cli as cli_mod
from leadforge.cli import app
from leadforge.profiles import ensure_schema, get_profile, list_profiles


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod.settings, "data_dir", tmp_path)
    db = tmp_path / "cache" / "leadforge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.close()
    return tmp_path


@pytest.fixture
def sample_resume(tmp_path) -> Path:
    p = tmp_path / "resume.txt"
    p.write_text(
        "Senior Software Engineer with Python, SQL, pandas, and machine learning. "
        "Seeking Data Analyst or Software Engineer roles. AWS, Docker, Git.",
        encoding="utf-8",
    )
    return p


def test_ingest_without_track_writes_profile_json_only(isolated_settings, sample_resume):
    runner = CliRunner()
    result = runner.invoke(app, ["profile", "ingest", str(sample_resume)])
    assert result.exit_code == 0, result.output
    assert (isolated_settings / "profile.json").exists()
    conn = sqlite3.connect(isolated_settings / "cache" / "leadforge.db")
    conn.row_factory = sqlite3.Row
    profiles = list_profiles(conn)
    conn.close()
    assert profiles == []


def test_ingest_with_track_persists_row(isolated_settings, sample_resume):
    runner = CliRunner()
    result = runner.invoke(
        app, ["profile", "ingest", str(sample_resume), "--track", "ai_ml"]
    )
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(isolated_settings / "cache" / "leadforge.db")
    conn.row_factory = sqlite3.Row
    p = get_profile(conn, "ai_ml")
    conn.close()
    assert p is not None
    assert p.track == "ai_ml"
    assert "python" in [s.lower() for s in p.skills]


def test_ingest_with_track_is_idempotent(isolated_settings, sample_resume):
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(
            app, ["profile", "ingest", str(sample_resume), "--track", "swe"]
        )
        assert result.exit_code == 0
    conn = sqlite3.connect(isolated_settings / "cache" / "leadforge.db")
    conn.row_factory = sqlite3.Row
    profiles = list_profiles(conn)
    conn.close()
    assert len(profiles) == 1
    assert profiles[0].profile_id == "swe"


def test_ingest_with_track_records_sha256(isolated_settings, sample_resume):
    runner = CliRunner()
    result = runner.invoke(
        app, ["profile", "ingest", str(sample_resume), "--track", "swe"]
    )
    assert result.exit_code == 0
    conn = sqlite3.connect(isolated_settings / "cache" / "leadforge.db")
    conn.row_factory = sqlite3.Row
    p = get_profile(conn, "swe")
    conn.close()
    assert p is not None
    assert p.resume_sha256
    assert len(p.resume_sha256) == 64
    assert p.resume_path == str(sample_resume)


def test_ingest_multiple_tracks(isolated_settings, sample_resume):
    runner = CliRunner()
    for track in ("ai_ml", "swe", "data_analyst"):
        result = runner.invoke(
            app, ["profile", "ingest", str(sample_resume), "--track", track]
        )
        assert result.exit_code == 0
    conn = sqlite3.connect(isolated_settings / "cache" / "leadforge.db")
    conn.row_factory = sqlite3.Row
    profiles = list_profiles(conn)
    conn.close()
    assert len(profiles) == 3
    assert sorted(p.track for p in profiles) == ["ai_ml", "data_analyst", "swe"]
def test_skill_extraction_uses_word_boundaries():
    """Substring matches should not trigger skills (e.g. 'r' inside 'years')."""
    from leadforge.stages.profile import extract_profile

    text = "3 years of experience with Python and SQL."
    profile = extract_profile(text)
    # "years" contains the letter r, but should not produce an "r" skill
    assert "r" not in profile.skills
    assert "python" in profile.skills
    assert "sql" in profile.skills


def test_skill_extraction_matches_real_words():
    """Actual skill words should match with word boundaries."""
    from leadforge.stages.profile import extract_profile

    text = "Proficient in Python, SQL, AWS, and Kubernetes."
    profile = extract_profile(text)
    assert "python" in profile.skills
    assert "sql" in profile.skills
    assert "aws" in profile.skills
    assert "kubernetes" in profile.skills


def test_skill_extraction_handles_symbols():
    """Skills with symbols (c++, c#, node.js) should still match."""
    from leadforge.stages.profile import extract_profile

    text = "Experienced with C++ and Node.js and C#."
    profile = extract_profile(text)
    assert "c++" in profile.skills
    assert "node.js" in profile.skills
    assert "c#" in profile.skills


def test_skill_extraction_does_not_match_go_inside_algorithms():
    """'go' should not match inside 'algorithms'."""
    from leadforge.stages.profile import extract_profile

    text = "Designed efficient algorithms for sorting."
    profile = extract_profile(text)
    assert "go" not in profile.skills
    assert "golang" not in profile.skills