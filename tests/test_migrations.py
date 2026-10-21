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


def test_all_migrations_are_str_or_callable():
    for i, m in enumerate(MIGRATIONS, start=1):
        assert isinstance(m, str) or callable(m), (
            f"migration {i} is {type(m).__name__}, expected str or callable"
        )