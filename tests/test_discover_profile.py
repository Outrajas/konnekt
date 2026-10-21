"""Tests for discover --profile flag and profile loader."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from leadforge import cli as cli_mod
from leadforge.cli import _load_profile_for_discover, app
from leadforge.profiles import Profile, ensure_schema, upsert_profile


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
def companies_csv(tmp_path) -> Path:
    p = tmp_path / "input" / "companies.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("name,domain\nAcme,acme.test\n", encoding="utf-8")
    return p


def test_discover_dry_run_reports_profile_name(isolated_settings, companies_csv):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["discover", "--companies", str(companies_csv), "--dry-run", "--profile", "ai_ml"],
    )
    assert result.exit_code == 0, result.output
    assert "profile: ai_ml" in result.output


def test_discover_dry_run_without_profile_does_not_mention_profile(
    isolated_settings, companies_csv
):
    runner = CliRunner()
    result = runner.invoke(
        app, ["discover", "--companies", str(companies_csv), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "profile:" not in result.output


def test_load_profile_from_db(isolated_settings):
    conn = sqlite3.connect(isolated_settings / "cache" / "leadforge.db")
    conn.row_factory = sqlite3.Row
    upsert_profile(
        conn,
        Profile(
            profile_id="ai_ml",
            track="ai_ml",
            skills=["python", "pytorch"],
            target_titles=["ml engineer"],
            localities=["India", "remote"],
        ),
    )
    conn.close()

    op = _load_profile_for_discover("ai_ml")
    assert op.skills == ["python", "pytorch"]
    assert op.target_titles == ["ml engineer"]
    assert op.location_preferences == ["India", "remote"]


def test_load_profile_by_track_alias(isolated_settings):
    conn = sqlite3.connect(isolated_settings / "cache" / "leadforge.db")
    conn.row_factory = sqlite3.Row
    upsert_profile(
        conn,
        Profile(
            profile_id="my_ai",
            track="ai_ml",
            skills=["pytorch"],
            target_titles=["ai engineer"],
            localities=[],
        ),
    )
    conn.close()

    op = _load_profile_for_discover("ai_ml")
    assert op.skills == ["pytorch"]


def test_load_profile_missing_raises_exit_2(isolated_settings):
    with pytest.raises(typer.Exit) as exc:
        _load_profile_for_discover("does_not_exist")
    assert exc.value.exit_code == 2


def test_load_profile_legacy_reads_profile_json(isolated_settings):
    (isolated_settings / "profile.json").write_text(
        json.dumps(
            {
                "skills": ["python"],
                "target_titles": ["swe"],
                "location_preferences": ["India"],
            }
        ),
        encoding="utf-8",
    )
    op = _load_profile_for_discover(None)
    assert op.skills == ["python"]
    assert op.target_titles == ["swe"]


def test_load_profile_legacy_missing_raises_exit_2(isolated_settings):
    with pytest.raises(typer.Exit) as exc:
        _load_profile_for_discover(None)
    assert exc.value.exit_code == 2