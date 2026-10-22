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