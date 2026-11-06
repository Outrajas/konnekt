"""Tests for the migration framework (callables + fresh-DB provisioning)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from leadforge.db import MIGRATIONS, connect, init_schema, schema_version


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test.db"


def test_fresh_db_runs_all_migrations(temp_db):
    conn = connect(temp_db)
    try:
        init_schema(conn)
        assert schema_version(conn) == len(MIGRATIONS)
    finally:
        conn.close()


def test_fresh_db_has_job_intel_columns(temp_db):
    conn = connect(temp_db)
    try:
        init_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(job)").fetchall()}
        for required in (
            "description", "salary_min", "salary_max", "salary_currency",
            "salary_period", "salary_source", "tech_stack_json",
            "salary_raw", "salary_evidence_url", "salary_retrieved_at",
            "salary_extraction_method", "salary_search_status",
            "reputation_score", "reputation_signals_json", "enriched_at",
        ):
            assert required in cols, f"missing column {required}"
    finally:
        conn.close()


def test_init_schema_is_idempotent(temp_db):
    conn = connect(temp_db)
    try:
        init_schema(conn)
        v1 = schema_version(conn)
        init_schema(conn)
        v2 = schema_version(conn)
        assert v1 == v2
    finally:
        conn.close()


def test_callable_migration_skips_existing_columns(temp_db):
    """A callable migration that adds columns must be a no-op if they exist."""
    conn = connect(temp_db)
    try:
        init_schema(conn)
        # Drop the version back to just-before-last, then re-run.
        # The last migration is our callable; it must be safe to run even
        # though columns already exist.
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (len(MIGRATIONS) - 1,))
        conn.commit()
        init_schema(conn)  # should not raise
        assert schema_version(conn) == len(MIGRATIONS)
    finally:
        conn.close()


def test_legacy_v10_database_receives_live_run_tables_and_columns(temp_db):
    """Existing databases get schema added after the original v10 release."""
    conn = connect(temp_db)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        for version, migration in enumerate(MIGRATIONS[:10], start=1):
            if isinstance(migration, str):
                conn.executescript(migration)
            else:
                migration(conn)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
            conn.commit()

        assert schema_version(conn) == 10
        init_schema(conn)

        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(job)").fetchall()}
        assert "salary_search_status" in job_columns
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='draft'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_all_migrations_are_str_or_callable():
    for i, m in enumerate(MIGRATIONS, start=1):
        assert isinstance(m, str) or callable(m), (
            f"migration {i} is {type(m).__name__}, expected str or callable"
        )
def test_fresh_db_has_profile_id_columns(temp_db):
    """profile_id must exist on all per-profile tables in a fresh DB."""
    conn = connect(temp_db)
    try:
        init_schema(conn)
        for table in (
            "company",
            "job",
            "person",
            "email_candidate",
            "outreach",
            "run_log",
        ):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "profile_id" in cols, f"{table}.profile_id missing"
    finally:
        conn.close()