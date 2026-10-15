from pathlib import Path

from typer.testing import CliRunner

from leadforge import cli


def test_verify_refuses_before_opening_csv_without_real_mail_from(monkeypatch) -> None:
    monkeypatch.setattr(cli.settings, "smtp_mail_from", "verify@yourdomain.com")

    result = CliRunner().invoke(cli.app, ["verify", "missing.csv"])

    assert result.exit_code == 2
    assert "SMTP checks refused" in result.output
    assert "placeholder" in result.output


def test_verify_refuses_empty_mail_from_before_opening_csv(monkeypatch) -> None:
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
