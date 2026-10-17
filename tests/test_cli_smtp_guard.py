from pathlib import Path

import pytest
from typer.testing import CliRunner

from leadforge import cli


def test_verify_refuses_before_opening_csv_without_real_mail_from(monkeypatch) -> None:
    monkeypatch.setattr(cli.settings, "smtp_verify_mode", "domain")
    monkeypatch.setattr(cli.settings, "smtp_mail_from", "verify@yourdomain.com")

    result = CliRunner().invoke(cli.app, ["verify", "missing.csv"])

    assert result.exit_code == 2
    assert "SMTP checks refused" in result.output
    assert "placeholder" in result.output


def test_verify_refuses_empty_mail_from_before_opening_csv(monkeypatch) -> None:
    monkeypatch.setattr(cli.settings, "smtp_verify_mode", "domain")
    monkeypatch.setattr(cli.settings, "smtp_mail_from", None)

    result = CliRunner().invoke(cli.app, ["verify", str(Path("missing.csv"))])

    assert result.exit_code == 2
    assert "SMTP checks refused" in result.output


def test_discover_dry_run_only_prints_plan(tmp_path, monkeypatch) -> None:
    companies = tmp_path / "companies.csv"
    companies.write_text("name,domain\nAcme,acme.example\n", encoding="utf-8")

    class NoNetwork:
        def __init__(self, *args, **kwargs):
            raise AssertionError("dry-run must not create a fetcher")

    monkeypatch.setattr(cli, "Fetcher", NoNetwork)
    result = CliRunner().invoke(cli.app, ["discover", "--companies", str(companies), "--dry-run"])

    assert result.exit_code == 0
    assert "no requests made" in result.output
    assert "acme.example" in result.output


def test_discover_limit_selects_first_companies_for_preview(tmp_path) -> None:
    companies = tmp_path / "companies.csv"
    companies.write_text(
        "name,domain\nFirst,first.example\nSecond,second.example\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli.app,
        ["discover", "--companies", str(companies), "--limit", "1", "--dry-run"],
    )
    assert result.exit_code == 0
    assert "first.example" in result.output
    assert "second.example" not in result.output


def test_people_replay_runs_without_live_operator_inputs(monkeypatch) -> None:
    class NoNetwork:
        def __init__(self, *args, **kwargs):
            raise AssertionError("replay must not construct a fetcher")

    monkeypatch.setattr(cli, "Fetcher", NoNetwork)
    result = CliRunner().invoke(
        cli.app,
        [
            "people",
            "--company",
            "Acme",
            "--domain",
            "acme.example",
            "--replay",
            "data/fixtures/people/sample",
        ],
    )

    assert result.exit_code == 0
    assert "Jane Doe" in result.output
    assert "Riley Chen" in result.output


def test_live_walk_refuses_without_operator_inputs(monkeypatch) -> None:
    class NoNetwork:
        def __init__(self, *args, **kwargs):
            raise AssertionError("walk must gate before constructing its fetcher")

    monkeypatch.setattr(cli, "Fetcher", NoNetwork)
    result = CliRunner().invoke(
        cli.app,
        [
            "walk",
            "--company-domain",
            "acme.example",
            "--first",
            "Jane",
            "--last",
            "Doe",
        ],
    )

    assert result.exit_code == 2
    assert "provide data/input/companies.csv" in result.output


def test_doctor_warns_on_empty_mail_from_only_in_domain_mode(monkeypatch) -> None:
    async def reachable_port25(**_kwargs):
        return cli.Port25Status.REACHABLE

    monkeypatch.setattr(cli.settings, "smtp_verify_mode", "domain")
    monkeypatch.setattr(cli.settings, "smtp_mail_from", None)
    monkeypatch.setattr(cli, "check_port25_status", reachable_port25)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "SMTP verify mode: domain" in result.output
    assert "Warning:" in result.output
    assert "SMTP MAIL FROM variable:" in result.output
    assert "as seen from this process" in result.output


@pytest.mark.parametrize("mode", ["off", "null_sender"])
def test_doctor_suppresses_sender_warning_and_never_prints_setting_values(
    monkeypatch, mode
) -> None:
    async def reachable_port25(**_kwargs):
        return cli.Port25Status.REACHABLE

    monkeypatch.setattr(cli.settings, "smtp_verify_mode", mode)
    monkeypatch.setattr(cli.settings, "smtp_mail_from", "private-sender-value@example.invalid")
    monkeypatch.setattr(cli, "check_port25_status", reachable_port25)
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert f"SMTP verify mode: {mode}" in result.output
    assert "private-sender-value" not in result.output
    assert "Warning: SMTP MAIL FROM" not in result.output


def test_doctor_reports_legacy_and_duplicate_env_names_without_values(monkeypatch) -> None:
    async def reachable_port25(**_kwargs):
        return cli.Port25Status.REACHABLE

    monkeypatch.setattr(cli, "check_port25_status", reachable_port25)
    monkeypatch.setattr(cli, "setting_env_name", lambda _field: "LEADFORGE_LLM_API_KEY")
    monkeypatch.setattr(
        cli,
        "setting_env_names",
        lambda field: ("LLM_API_KEY", "LEADFORGE_LLM_API_KEY") if field == "llm_api_key" else (),
    )
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "legacy environment variable name supplied: LLM_API_KEY" in result.output
    assert "both legacy and prefixed names are set" in result.output
    assert "External tools" not in result.output
    assert "secret" not in result.output
