import sqlite3

import pytest

from leadforge.db import connect, init_schema, schema_version
from leadforge.repo import (
    add_candidate,
    add_evidence,
    add_job,
    add_page,
    add_person,
    bump_domain_count,
    get_domain_pattern,
    get_domain_state,
    pause_domain,
    set_domain_pattern,
    upsert_company,
)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    init_schema(connection)
    yield connection
    connection.close()


def test_schema_is_idempotent_and_foreign_keys_are_enabled(conn) -> None:
    init_schema(conn)
    assert schema_version(conn) == 2
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_unique_constraints_are_enforced(conn) -> None:
    company_id = upsert_company(conn, name="Example", domain="example.test")
    person_id = add_person(conn, company_id=company_id, first_name="Jane")
    add_candidate(
        conn, person_id=person_id, email="jane@example.test", source="test", evidence_tier="C"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO company(name, domain, created_at) VALUES (?, ?, ?)",
            ("Duplicate", "example.test", "2026-01-01T00:00:00+00:00"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO email_candidate(person_id, email, source, evidence_tier, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (person_id, "jane@example.test", "test", "C", "2026-01-01T00:00:00+00:00"),
        )


def test_repository_helpers_round_trip(conn) -> None:
    company_id = upsert_company(conn, name="Example", domain="example.test", source="fixture")
    assert upsert_company(conn, name="Example Co", domain="example.test") == company_id
    job_id = add_job(
        conn,
        company_id=company_id,
        title="Engineer",
        url="https://example.test/jobs/1",
        source="fixture",
    )
    person_id = add_person(conn, company_id=company_id, first_name="Jane", last_name="Doe")
    page_id = add_page(
        conn, url="https://example.test/team", url_hash="hash1", host="example.test",
        tier=1, expires_at="2026-02-01T00:00:00+00:00",
    )
    evidence_id = add_evidence(
        conn, person_id=person_id, page_id=page_id, kind="name_title", value="Jane Doe",
        professional=True,
    )
    candidate_id = add_candidate(
        conn, person_id=person_id, email="jane@example.test", source="published",
        evidence_tier="A", found_on_web=True,
    )
    set_domain_pattern(conn, domain="example.test", pattern="first", sample_count=2)
    pattern = get_domain_pattern(conn, "example.test")
    assert pattern is not None and pattern["pattern"] == "first"
    assert get_domain_state(conn, "example.test") is None
    assert all((job_id, person_id, page_id, evidence_id, candidate_id))


def test_domain_state_count_rollover_and_pause(conn) -> None:
    assert bump_domain_count(conn, "example.test", now="2026-01-01T12:00:00+00:00") == 1
    assert bump_domain_count(conn, "example.test", now="2026-01-01T12:01:00+00:00") == 2
    assert bump_domain_count(conn, "example.test", now="2026-01-02T00:00:00+00:00") == 1
    paused_until = pause_domain(
        conn, "example.test", 48, "challenge", now="2026-01-02T00:00:00+00:00"
    )
    state = get_domain_state(conn, "example.test")
    assert state is not None
    assert state["paused_until"] == paused_until
    assert state["pause_reason"] == "challenge"
