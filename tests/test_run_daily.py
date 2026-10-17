import json

from typer.testing import CliRunner

from leadforge import cli
from leadforge.config import Settings
from leadforge.web.errors import Blocked


def _settings(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        input_dir=tmp_path / "data" / "input",
        output_dir=tmp_path / "data" / "output",
        cache_dir=tmp_path / "data" / "cache",
        db_path=tmp_path / "data" / "cache" / "leadforge.db",
    )
    settings.input_dir.mkdir(parents=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.input_dir / "companies.csv").write_text(
        "name,domain\nAcme,acme.invalid\nBeta,beta.invalid\n", encoding="utf-8"
    )
    (settings.data_dir / "profile.json").write_text("{}", encoding="utf-8")
    return settings


def test_run_daily_dry_run_creates_no_lock_or_network_client(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "settings", settings)

    result = CliRunner().invoke(cli.app, ["run-daily", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "no network requests" in result.output
    assert not (settings.cache_dir / "run-daily.lock").exists()


def test_run_daily_persists_stage_completion_and_resumes(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "settings", settings)
    calls = []
    monkeypatch.setattr(cli, "discover", lambda *args, **kwargs: calls.append("discover"))
    monkeypatch.setattr(cli, "people", lambda *args, **kwargs: calls.append("people"))
    contacts = settings.cache_dir / "contacts.csv"
    monkeypatch.setattr(cli, "_daily_contacts_csv", lambda *args: contacts)
    monkeypatch.setattr(cli, "enrich", lambda *args, **kwargs: calls.append("enrich"))
    monkeypatch.setattr(cli, "report", lambda *args, **kwargs: calls.append("report"))

    runner = CliRunner()
    first = runner.invoke(cli.app, ["run-daily"])
    first_calls = list(calls)
    second = runner.invoke(cli.app, ["run-daily"])
    state = json.loads((settings.cache_dir / "run-daily-state.json").read_text())

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert first_calls == ["discover", "people", "people", "enrich", "report"]
    assert calls == first_calls
    assert state["completed"] == ["discover", "enrich", "people", "report"]
    assert not (settings.cache_dir / "run-daily.lock").exists()


def test_run_daily_stops_on_fetch_block_and_releases_lock(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "settings", settings)

    def blocked(*args, **kwargs):
        raise Blocked("blocked response with page contents")

    monkeypatch.setattr(cli, "discover", blocked)
    result = CliRunner().invoke(cli.app, ["run-daily"])

    assert result.exit_code == 1
    assert "Blocked" in result.output
    assert "page contents" not in result.output
    assert not (settings.cache_dir / "run-daily.lock").exists()
