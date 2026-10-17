import csv
from datetime import date

from typer.testing import CliRunner

from leadforge import cli
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.outreach import daily_cap, is_suppressed_or_contacted


def _seed_candidate(settings: Settings, *, email: str = "jane@example.invalid") -> int:
    conn = connect(settings.db_path)
    init_schema(conn)
    company_id = conn.execute(
        "INSERT INTO company(name,domain,created_at) VALUES(?,?,?)",
        ("Example", "example.invalid", "2026-10-01T00:00:00+00:00"),
    ).lastrowid
    person_id = conn.execute(
        "INSERT INTO person(company_id,first_name,last_name,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            company_id,
            "Jane",
            "Doe",
            "found",
            "2026-10-01T00:00:00+00:00",
            "2026-10-01T00:00:00+00:00",
        ),
    ).lastrowid
    conn.execute(
        "INSERT INTO email_candidate(person_id,email,source,evidence_tier,"
        "verification_verdict,confidence,created_at) "
        "VALUES(?,?,?,'A','unknown',0.9,?)",
        (person_id, email, "public_web", "2026-10-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return int(person_id)


def test_outreach_daily_ramp_is_5_10_then_20(tmp_path) -> None:
    conn = connect(tmp_path / "outreach.db")
    init_schema(conn)
    assert daily_cap(conn, date(2026, 10, 1)) == 5
    conn.execute(
        "INSERT INTO company(name,domain,created_at) "
        "VALUES('Example','example.invalid','2026-10-01')"
    )
    conn.execute(
        "INSERT INTO person(company_id,first_name,last_name,created_at,updated_at) "
        "VALUES(1,'Jane','Doe','2026-10-01','2026-10-01')"
    )
    conn.execute(
        "INSERT INTO outreach(person_id,email,sent_at,channel) "
        "VALUES(1,'jane@example.invalid','2026-10-01T10:00:00+00:00','email')"
    )
    assert daily_cap(conn, date(2026, 10, 7)) == 5
    assert daily_cap(conn, date(2026, 10, 8)) == 10
    assert daily_cap(conn, date(2026, 10, 15)) == 20
    conn.close()


def test_mark_sent_suppresses_contact_from_today_and_outcome_can_be_recorded(
    tmp_path, monkeypatch
) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "db.sqlite", data_dir=tmp_path / "data")
    person_id = _seed_candidate(settings)
    monkeypatch.setattr(cli, "settings", settings)
    runner = CliRunner()

    sent = runner.invoke(cli.app, ["mark-sent", str(person_id), "jane@example.invalid"])
    listed = runner.invoke(cli.app, ["today"])
    outcome = runner.invoke(
        cli.app, ["mark-outcome", str(person_id), "jane@example.invalid", "reply"]
    )

    assert sent.exit_code == 0, sent.output
    assert listed.exit_code == 0, listed.output
    assert "Jane Doe" not in listed.output
    assert outcome.exit_code == 0, outcome.output
    conn = connect(settings.db_path)
    row = conn.execute("SELECT outcome FROM outreach WHERE person_id=?", (person_id,)).fetchone()
    assert row["outcome"] == "reply"
    assert is_suppressed_or_contacted(conn, "jane@example.invalid", person_id=person_id)
    conn.close()


def test_mark_sent_removes_preexisting_review_row(tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "db.sqlite",
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
    )
    person_id = _seed_candidate(settings)
    settings.output_dir.mkdir(parents=True)
    (settings.output_dir / "review.csv").write_text(
        "email,company,first_name,last_name,evidence_tier,confidence,provenance,approved\n"
        "jane@example.invalid,Example,Jane,Doe,A,0.90,https://example.invalid/team,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "settings", settings)

    result = CliRunner().invoke(cli.app, ["mark-sent", str(person_id), "jane@example.invalid"])

    assert result.exit_code == 0, result.output
    with (settings.output_dir / "review.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    assert rows == []


def test_suppression_hides_domain_candidates_from_today(tmp_path, monkeypatch) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "db.sqlite", data_dir=tmp_path / "data")
    _seed_candidate(settings)
    monkeypatch.setattr(cli, "settings", settings)
    result = CliRunner().invoke(cli.app, ["suppress", "example.invalid"])
    today = CliRunner().invoke(cli.app, ["today"])
    assert result.exit_code == 0
    assert today.exit_code == 0
    assert "jane@example.invalid" not in today.output


def test_backup_and_typed_reset_recreate_empty_schema(tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "data" / "cache",
        db_path=tmp_path / "data" / "cache" / "leadforge.db",
    )
    person_id = _seed_candidate(settings)
    monkeypatch.setattr(cli, "settings", settings)
    result = CliRunner().invoke(
        cli.app, ["reset-db", "--confirm"], input="RESET LEADFORGE DATABASE\n"
    )
    assert result.exit_code == 0, result.output
    backups = list((settings.data_dir / "backups").glob("*.db"))
    assert len(backups) == 1
    backup_conn = connect(backups[0])
    assert (
        backup_conn.execute("SELECT COUNT(*) FROM person WHERE id=?", (person_id,)).fetchone()[0]
        == 1
    )
    backup_conn.close()
    conn = connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 0
    conn.close()


def test_report_warns_when_bounce_rate_reaches_three_percent(tmp_path, monkeypatch) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "db.sqlite", data_dir=tmp_path / "data")
    person_id = _seed_candidate(settings)
    conn = connect(settings.db_path)
    conn.execute(
        "INSERT INTO outreach(person_id,email,sent_at,channel,outcome) "
        "VALUES(?,?,datetime('now'),'email','bounce')",
        (person_id, "jane@example.invalid"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(cli, "settings", settings)
    result = CliRunner().invoke(cli.app, ["report"])
    assert result.exit_code == 0, result.output
    assert "bounce alert" in result.output.casefold()
