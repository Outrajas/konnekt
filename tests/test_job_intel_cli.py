"""Tests for job-intel CLI wiring."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from leadforge import cli as cli_mod
from leadforge.cli import app


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    from leadforge.db import connect, init_schema

    db = tmp_path / "cache" / "leadforge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_mod.settings, "data_dir", tmp_path)
    try:
        monkeypatch.setattr(cli_mod.settings, "db_path", db)
    except (AttributeError, TypeError):
        pass
    conn = connect(db)
    init_schema(conn)
    conn.close()
    return tmp_path


def test_discover_help_shows_intel_flag():
    runner = CliRunner()
    result = runner.invoke(app, ["discover", "--help"])
    assert result.exit_code == 0
    assert "--intel" in result.output


def test_job_intel_help_shows_domain_and_all():
    runner = CliRunner()
    result = runner.invoke(app, ["job-intel", "--help"])
    assert result.exit_code == 0
    assert "--domain" in result.output
    assert "--all" in result.output


def test_job_intel_requires_domain_or_all(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(app, ["job-intel"])
    assert result.exit_code == 2


def test_job_intel_all_no_jobs(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(app, ["job-intel", "--all"])
    assert result.exit_code == 0
    assert "no jobs matched" in result.output


def test_job_intel_domain_no_jobs(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(app, ["job-intel", "--domain", "nothing.test"])
    assert result.exit_code == 0
    assert "no jobs matched" in result.output
# ---------------- discover --board CLI tests ----------------

def test_discover_help_shows_board_flag():
    runner = CliRunner()
    result = runner.invoke(app, ["discover", "--help"])
    assert result.exit_code == 0
    assert "--board" in result.output


def test_discover_help_shows_skip_companies():
    runner = CliRunner()
    result = runner.invoke(app, ["discover", "--help"])
    assert result.exit_code == 0
    assert "--skip-companies" in result.output


def test_discover_help_shows_board_india():
    runner = CliRunner()
    result = runner.invoke(app, ["discover", "--help"])
    assert result.exit_code == 0
    assert "--board-india" in result.output


def test_discover_invalid_entry_level_rejected(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["discover", "--skip-companies", "--board", "wellfound",
         "--board-entry-level", "invalid"],
    )
    assert result.exit_code == 2
    assert "must be strict or lenient" in result.output


def test_discover_no_companies_no_skip_errors(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(app, ["discover", "--board", "wellfound"])
    assert result.exit_code == 2
    assert "--companies is required" in result.output


def test_discover_board_wellfound_dry_run(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["discover", "--skip-companies", "--board", "wellfound", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "board: wellfound" in result.output
    assert "india=True" in result.output


def test_discover_board_wellfound_no_india_dry_run(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["discover", "--skip-companies", "--board", "wellfound",
         "--no-board-india", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "india=False" in result.output


def test_discover_board_strict_entry_level_dry_run(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["discover", "--skip-companies", "--board", "wellfound",
         "--board-entry-level", "strict", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "entry_level=strict" in result.output


def test_discover_multiple_boards_dry_run(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["discover", "--skip-companies",
         "--board", "wellfound", "--board", "naukri", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "board: wellfound" in result.output
    assert "board: naukri" in result.output