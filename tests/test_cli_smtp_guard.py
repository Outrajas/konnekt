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
