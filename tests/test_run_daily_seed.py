import csv

import pytest

from leadforge import cli


def _companies(path, count=8):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "domain"])
        writer.writerows((f"Fictional {i}", f"fictional{i}.invalid") for i in range(count))


@pytest.mark.parametrize("empty_file", [False, True])
def test_run_daily_seed_runs_when_company_file_missing_or_empty(tmp_path, monkeypatch, empty_file):
    companies = tmp_path / "companies.csv"
    if empty_file:
        companies.write_text("name,domain\n", encoding="utf-8")
    called = []

    def fake_seed(**kwargs):
        called.append(kwargs)
        _companies(companies)
        return [object() for _ in range(8)], {}

    monkeypatch.setattr(cli, "seed_sync", fake_seed)
    assert cli._seed_companies_if_needed(companies, enabled=True, dry_run=False)
    assert called == [{"target": 25, "dry_run": False}]
    assert len(cli.read_companies(companies)) == 8


def test_run_daily_seed_dry_run_does_not_create_company_file(tmp_path, monkeypatch):
    companies = tmp_path / "companies.csv"
    called = []
    monkeypatch.setattr(cli, "seed_sync", lambda **kwargs: called.append(kwargs) or ([], {}))
    assert cli._seed_companies_if_needed(companies, enabled=True, dry_run=True)
    assert called == [{"target": 25, "dry_run": True}]
    assert not companies.exists()


def test_run_daily_seed_is_skipped_when_companies_already_exist(tmp_path, monkeypatch):
    companies = tmp_path / "companies.csv"
    _companies(companies, count=2)

    def unexpected_seed(**kwargs):
        raise AssertionError("existing company rows must skip seeding")

    monkeypatch.setattr(cli, "seed_sync", unexpected_seed)
    assert not cli._seed_companies_if_needed(companies, enabled=True, dry_run=False)


def test_run_daily_seed_stops_when_fewer_than_eight_are_accepted(tmp_path, monkeypatch):
    companies = tmp_path / "companies.csv"
    monkeypatch.setattr(cli, "seed_sync", lambda **kwargs: ([object()] * 7, {}))
    with pytest.raises(cli.typer.Exit) as exc:
        cli._seed_companies_if_needed(companies, enabled=True, dry_run=False)
    assert exc.value.exit_code == 2
    assert not companies.exists()
