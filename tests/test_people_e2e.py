"""Tests for people_e2e stage."""
from __future__ import annotations

import asyncio
import json

import pytest

from leadforge import cli as cli_mod
from leadforge.db import connect, init_schema
from leadforge.models import PersonCandidate
from leadforge.stages.people_e2e import (
    _split_name,
    _upsert_person,
    find_people_for_company,
    load_domain_overrides,
    run_people_e2e,
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
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
    return cli_mod.settings


@pytest.fixture
def db_with_wellfound_job(settings):
    conn = connect(settings.db_path)
    try:
        now = "2026-10-27T00:00:00Z"
        conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Simplismart", "simplismart.wellfound", "wellfound", now),
        )
        cid = conn.execute(
            "SELECT id FROM company WHERE domain=?", ("simplismart.wellfound",)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO job (company_id, title, url, source, first_seen, last_seen, profile_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, "ML Engineer", "https://wellfound.com/jobs/1-ml", "wellfound", now, now, "ai_ml"),
        )
        conn.commit()
    finally:
        conn.close()
    return settings


# ---------------- _split_name ----------------


def test_split_name_basic():
    assert _split_name("Jane Smith") == ("Jane", "Smith")


def test_split_name_three_words():
    assert _split_name("Mary Jane Watson") == ("Mary", "Jane Watson")


def test_split_name_single():
    assert _split_name("Madonna") == ("Madonna", "")


def test_split_name_empty():
    assert _split_name("") == ("", "")


# ---------------- load_domain_overrides ----------------


def test_load_overrides_reads_file(tmp_path):
    p = tmp_path / "overrides.json"
    p.write_text(
        json.dumps({"simplismart.wellfound": "simplismart.ai", "_comment": "x"}),
        encoding="utf-8",
    )
    overrides = load_domain_overrides(p)
    assert overrides == {"simplismart.wellfound": "simplismart.ai"}


def test_load_overrides_missing_file(tmp_path):
    assert load_domain_overrides(tmp_path / "nope.json") == {}


def test_load_overrides_invalid_json(tmp_path):
    p = tmp_path / "overrides.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_domain_overrides(p) == {}


# ---------------- _upsert_person ----------------


def test_upsert_person_creates_row(settings):
    conn = connect(settings.db_path)
    try:
        cid = conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Acme", "acme.test", "test", "2026-10-27T00:00:00Z"),
        ).lastrowid
        cand = PersonCandidate(
            name="Jane Smith", title="Head of Talent", company="Acme",
            url="https://acme.test/team", snippet="", source="team-page",
        )
        created = _upsert_person(conn, cid, cand, "ai_ml")
        conn.commit()
        assert created is True
        row = conn.execute(
            "SELECT * FROM person WHERE company_id=?", (cid,)
        ).fetchone()
        assert row is not None
        assert row["first_name"] == "Jane"
        assert row["last_name"] == "Smith"
        assert row["title"] == "Head of Talent"
    finally:
        conn.close()


def test_upsert_person_dedupes(settings):
    conn = connect(settings.db_path)
    try:
        cid = conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Acme", "acme.test", "test", "2026-10-27T00:00:00Z"),
        ).lastrowid
        cand = PersonCandidate(
            name="Jane Smith", title="Head of Talent", company="Acme",
            url="https://acme.test/team", snippet="", source="team-page",
        )
        created1 = _upsert_person(conn, cid, cand, "ai_ml")
        created2 = _upsert_person(conn, cid, cand, "ai_ml")
        conn.commit()
        assert created1 is True
        assert created2 is False
        count = conn.execute(
            "SELECT COUNT(*) FROM person WHERE company_id=?", (cid,)
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


# ---------------- find_people_for_company ----------------


class _FakeFetcher:
    def __init__(self, pages: dict):
        self.pages = pages
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append(url)
        if url in self.pages:
            class R:
                def __init__(self, h): self.html = h
            return R(self.pages[url])
        from leadforge.web.errors import FetchError
        raise FetchError(f"not found {url}")


def test_find_people_skips_wellfound_placeholder():
    fake = _FakeFetcher({})
    people, source, note = asyncio.run(
        find_people_for_company(
            "Simplismart", "simplismart.wellfound", real_domain=None, fetcher=fake
        )
    )
    assert people == []
    assert source == "override_missing"
    assert "no real domain" in note


def test_find_people_uses_real_domain():
    html = """
    <div class="team-member">
      <h3>Jane Smith</h3>
      <p class="title">Head of Talent</p>
    </div>
    """
    fake = _FakeFetcher({
        "https://simplismart.ai/team": html,
    })
    people, source, _ = asyncio.run(
        find_people_for_company(
            "Simplismart", "simplismart.wellfound",
            real_domain="simplismart.ai", fetcher=fake,
        )
    )
    assert source == "walker"
    assert len(people) >= 1
    assert people[0].name == "Jane Smith"


def test_find_people_agent_fallback_when_walker_empty():
    """Walker returns 0, agent should run. Use fake agent fetcher."""
    # Walker fetches /team,/about,... (all 404)
    # Agent fetches /about (also 404, but fake LLM would need to be set up)
    # This test just verifies the code path doesn't crash
    fake = _FakeFetcher({})
    people, source, note = asyncio.run(
        find_people_for_company(
            "Acme", "acme.test", real_domain="acme.test",
            fetcher=fake,
            agent_max_pages=1,
            agent_max_tokens=100,  # tight so it exits fast
        )
    )
    # Empty agent result expected for a fetcher with no pages
    assert people == []
    assert source in ("agent", "fetch_error")


# ---------------- run_people_e2e ----------------


def test_run_e2e_dry_run_does_not_persist(db_with_wellfound_job, tmp_path):
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps({"simplismart.wellfound": "simplismart.ai"}),
        encoding="utf-8",
    )
    fake = _FakeFetcher({
        "https://simplismart.ai/team": """
            <div class="team-member">
              <h3>Jane Smith</h3>
              <p class="title">Head of Talent</p>
            </div>
        """,
    })
    result = asyncio.run(
        run_people_e2e(
            db_with_wellfound_job,
            profile_id="ai_ml",
            max_companies=1,
            domain_overrides_path=overrides,
            dry_run=True,
            fetcher=fake,
        )
    )
    assert result.total_found >= 1
    assert result.total_persisted == 0
    conn = connect(db_with_wellfound_job.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_run_e2e_live_persists(db_with_wellfound_job, tmp_path):
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps({"simplismart.wellfound": "simplismart.ai"}),
        encoding="utf-8",
    )
    fake = _FakeFetcher({
        "https://simplismart.ai/team": """
            <div class="team-member">
              <h3>Jane Smith</h3>
              <p class="title">Head of Talent</p>
            </div>
        """,
    })
    result = asyncio.run(
        run_people_e2e(
            db_with_wellfound_job,
            profile_id="ai_ml",
            max_companies=1,
            domain_overrides_path=overrides,
            dry_run=False,
            fetcher=fake,
        )
    )
    assert result.total_found >= 1
    assert result.total_persisted >= 1
    conn = connect(db_with_wellfound_job.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        assert count >= 1
    finally:
        conn.close()


def test_run_e2e_no_jobs_returns_empty(settings):
    result = asyncio.run(
        run_people_e2e(settings, profile_id="ai_ml", max_companies=5)
    )
    assert result.total_found == 0
    assert result.total_persisted == 0
# ---------------- validation gate in _upsert_person ----------------

def test_upsert_person_rejects_nav_name(settings):
    """Person rows with nav-like names must not be persisted."""
    from leadforge.models import PersonCandidate

    conn = connect(settings.db_path)
    try:
        cid = conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Augury", "augury.com", "test", "2026-10-27T00:00:00Z"),
        ).lastrowid
        cand = PersonCandidate(
            name="Our Values", title="Head of Talent",  # title good, name bad
            company="Augury", url="https://augury.com/about",
            snippet="", source="team-page",
        )
        created = _upsert_person(conn, cid, cand, "ai_ml")
        conn.commit()
        assert created is False
        count = conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_upsert_person_rejects_no_title(settings):
    """A real name without a role is suspicious — reject."""
    from leadforge.models import PersonCandidate

    conn = connect(settings.db_path)
    try:
        cid = conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Augury", "augury.com", "test", "2026-10-27T00:00:00Z"),
        ).lastrowid
        cand = PersonCandidate(
            name="Jane Smith", title="",  # name good, title missing
            company="Augury", url="https://augury.com/about",
            snippet="", source="team-page",
        )
        created = _upsert_person(conn, cid, cand, "ai_ml")
        conn.commit()
        assert created is False
    finally:
        conn.close()


def test_upsert_person_rejects_title_without_role(settings):
    """A title that isn't a role keyword is not accepted."""
    from leadforge.models import PersonCandidate

    conn = connect(settings.db_path)
    try:
        cid = conn.execute(
            "INSERT INTO company (name, domain, source, created_at) VALUES (?, ?, ?, ?)",
            ("Augury", "augury.com", "test", "2026-10-27T00:00:00Z"),
        ).lastrowid
        cand = PersonCandidate(
            name="Jane Smith", title="Some Random Text",  # no role keyword
            company="Augury", url="https://augury.com/about",
            snippet="", source="team-page",
        )
        created = _upsert_person(conn, cid, cand, "ai_ml")
        conn.commit()
        assert created is False
    finally:
        conn.close()