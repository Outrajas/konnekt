from __future__ import annotations

import asyncio

from leadforge.db import connect, init_schema
from leadforge.models import SearchHit
from leadforge.stages.research import ResearchBudget, research_company


class FakeSearch:
    async def search(self, query: str):
        return [
            SearchHit(
                title="Jane Smith - Head of Talent at Acme",
                url="https://acme.test/team",
                snippet="Jane Smith - Head of Talent at Acme jane.smith@acme.test",
            )
        ]


class FakeFetcher:
    async def fetch(self, url: str, **kwargs):
        class Response:
            html = """
            <section class="team-member">
              <h3>Jane Smith</h3>
              <p class="title">Head of Talent</p>
              <a href="mailto:jane.smith@acme.test">email</a>
            </section>
            """

        return Response()


def test_research_persists_trajectory_and_email(tmp_path, monkeypatch):
    db = tmp_path / "cache" / "leadforge.db"
    settings = type("Settings", (), {"db_path": db, "searxng_base_url": None})()
    conn = connect(db)
    init_schema(conn)
    conn.close()

    import leadforge.stages.research as research

    monkeypatch.setattr(research, "_provider", lambda settings, fetcher: ("fake", FakeSearch()))
    result = asyncio.run(
        research_company(
            settings,
            "Acme",
            "acme.test",
            role="Head of Talent",
            fetcher=FakeFetcher(),
            llm_client=False,
            budget=ResearchBudget(max_queries=2, max_hits=5, max_fetches=2),
        )
    )

    assert result.stop_reason == "person_and_email_found"
    assert result.people[0].name == "Jane Smith"
    assert "jane.smith@acme.test" in result.emails
    conn = connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM research_run").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM research_event").fetchone()[0] >= 3
        assert conn.execute("SELECT COUNT(*) FROM research_edge").fetchone()[0] >= 2
        assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM email_candidate").fetchone()[0] == 1
        candidate = conn.execute(
            "SELECT evidence_tier, verification_method, verification_verdict "
            "FROM email_candidate"
        ).fetchone()
        assert tuple(candidate) == ("C", "unverified", "unknown")
    finally:
        conn.close()


def test_research_progress_reports_live_trajectory(tmp_path, monkeypatch):
    db = tmp_path / "cache" / "leadforge.db"
    settings = type("Settings", (), {"db_path": db, "searxng_base_url": None})()
    conn = connect(db)
    init_schema(conn)
    conn.close()

    import leadforge.stages.research as research

    monkeypatch.setattr(research, "_provider", lambda settings, fetcher: ("fake", FakeSearch()))
    events: list[str] = []
    asyncio.run(
        research_company(
            settings,
            "Acme",
            "acme.test",
            fetcher=FakeFetcher(),
            llm_client=False,
            progress=lambda kind, payload: events.append(kind),
            budget=ResearchBudget(max_queries=1, max_hits=1, max_fetches=1),
        )
    )

    assert events[:2] == ["research_start", "query"]
    assert "search_hit" in events
    assert "fetch" in events
    assert "person_clue" in events
    assert "email_clue" in events
    assert events[-1] == "research_stop"


def test_research_dry_run_does_not_network_or_write(tmp_path):
    db = tmp_path / "cache" / "leadforge.db"
    settings = type("Settings", (), {"db_path": db, "searxng_base_url": None})()

    class NoNetworkFetcher:
        async def fetch(self, *args, **kwargs):
            raise AssertionError("dry-run must not fetch")

    result = asyncio.run(
        research_company(
            settings,
            "Acme",
            "acme.example",
            fetcher=NoNetworkFetcher(),
            dry_run=True,
        )
    )

    assert result.stop_reason == "dry_run"
    assert not db.exists()
