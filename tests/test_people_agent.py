"""Tests for the agentic people finder (LLM-guided crawl)."""
from __future__ import annotations

import asyncio

import pytest

from leadforge.sources.people_agent import (
    AgentDecision,
    AgentRunResult,
    DecisionPerson,
    _attribution_ok,
    _candidate_links,
    _summarize_page,
    find_people_agentic,
)

# ---------------- fakes ----------------


class _FakeResponse:
    def __init__(self, html: str):
        self.html = html
        self.status_code = 200
        self.tier = 1
        self.final_url = ""


class _FakeFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str, **kwargs):
        self.calls.append(url)
        if url in self.pages:
            return _FakeResponse(self.pages[url])
        from leadforge.web.errors import FetchError
        raise FetchError(f"not found: {url}")


class _FakeLLM:
    """Mimics chat_json(system, user, schema=...) -> schema instance (async)."""

    def __init__(self, decisions: list[AgentDecision | dict | Exception]):
        self.decisions = decisions
        self.calls: list[tuple[str, str]] = []

    async def chat_json(self, system: str, user: str, *, schema=None, **kwargs):
        self.calls.append((system, user))
        if not self.decisions:
            return schema(done=True)
        decision = self.decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        if isinstance(decision, dict):
            return schema(**decision)
        return decision


# ---------------- unit tests ----------------


def test_summarize_page_prefers_headings():
    html = "<html><body><h2>Our Team</h2><p>Jane Smith</p></body></html>"
    s = _summarize_page(html, "https://acme.test/team")
    assert "Our Team" in s or "Jane Smith" in s


def test_summarize_skips_scripts():
    html = "<html><body><script>var secret = 'x';</script><p>Hello</p></body></html>"
    assert "secret" not in _summarize_page(html, "https://x.test")


def test_candidate_links_ranks_team_paths():
    html = """
    <a href="/team">Team</a>
    <a href="/blog">Blog</a>
    <a href="/people/jane">Jane</a>
    <a href="/pricing">Pricing</a>
    """
    links = _candidate_links(html, "https://acme.test/")
    assert "https://acme.test/team" in links
    assert "https://acme.test/blog" not in links
    assert "https://acme.test/pricing" not in links


def test_candidate_links_rejects_offhost():
    html = '<a href="https://twitter.com/acme">Twitter</a>'
    assert _candidate_links(html, "https://acme.test/") == []


def test_attribution_requires_literal_name():
    p = DecisionPerson(name="Jane Smith", title="CTO")
    assert _attribution_ok(p, "<p>Jane Smith</p><p>CTO</p>") is True
    assert _attribution_ok(p, "<p>Some other name</p>") is False


def test_attribution_requires_literal_title():
    p = DecisionPerson(name="Jane Smith", title="Wizard of Oz")
    assert _attribution_ok(p, "Jane Smith") is False


def test_attribution_stoplist_rejects_nav():
    p = DecisionPerson(name="Compare Features Across Plans")
    assert _attribution_ok(p, "Compare features across plans here") is False


# ---------------- agent loop tests ----------------


def test_agent_stops_on_done():
    pages = {
        "https://acme.test/team": "<h2>Jane Smith</h2><p>Head of Talent</p>",
    }
    fetcher = _FakeFetcher(pages)
    llm = _FakeLLM([AgentDecision(done=True, reasoning="found enough")])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team", llm_client=llm, fetcher=fetcher
        )
    )
    assert result.stop_reason.startswith("llm_done")
    assert result.pages_fetched == 1


def test_agent_follows_next_url():
    pages = {
        "https://acme.test/team": "<h2>Jane Smith</h2><p>Head of Talent</p>",
        "https://acme.test/team/engineering": "<h2>Raj Kumar</h2><p>Engineering Manager</p>",
    }
    fetcher = _FakeFetcher(pages)
    llm = _FakeLLM([
        AgentDecision(
            next_url="https://acme.test/team/engineering",
            done=False,
            reasoning="more engineers there",
        ),
        AgentDecision(done=True, reasoning="done"),
    ])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team", llm_client=llm, fetcher=fetcher
        )
    )
    assert result.pages_fetched == 2
    names = {p.name for p in result.people}
    assert "Jane Smith" in names
    assert "Raj Kumar" in names


def test_agent_llm_people_attribution_checked():
    pages = {
        "https://acme.test/team": "<p>Jane Smith</p><p>Head of Talent</p>",
    }
    fetcher = _FakeFetcher(pages)
    llm = _FakeLLM([
        AgentDecision(
            people=[
                DecisionPerson(name="Jane Smith", title="Head of Talent"),
                DecisionPerson(name="Hallucination Jones", title="CTO"),
            ],
            done=True,
        ),
    ])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team", llm_client=llm, fetcher=fetcher
        )
    )
    names = {p.name for p in result.people}
    assert "Jane Smith" in names
    assert "Hallucination Jones" not in names


def test_agent_dedupes_across_sources():
    pages = {
        "https://acme.test/team": "<h2>Jane Smith</h2><p>Head of Talent</p>",
    }
    fetcher = _FakeFetcher(pages)
    llm = _FakeLLM([
        AgentDecision(
            people=[DecisionPerson(name="Jane Smith", title="Head of Talent")],
            done=True,
        ),
    ])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team", llm_client=llm, fetcher=fetcher
        )
    )
    jane_count = sum(1 for p in result.people if p.name == "Jane Smith")
    assert jane_count == 1


def test_agent_respects_max_pages():
    pages = {
        f"https://acme.test/p{i}": "<p>nothing here</p>" for i in range(10)
    }
    fetcher = _FakeFetcher(pages)
    llm = _FakeLLM([
        AgentDecision(next_url=f"https://acme.test/p{i+1}") for i in range(10)
    ])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/p0",
            llm_client=llm, fetcher=fetcher, max_pages=3,
        )
    )
    assert result.pages_fetched <= 3


def test_agent_respects_max_tokens():
    pages = {"https://acme.test/team": "<p>x</p>"}
    fetcher = _FakeFetcher(pages)
    llm = _FakeLLM([AgentDecision(done=True, reasoning="stop")])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team",
            llm_client=llm, fetcher=fetcher, max_tokens=1,
        )
    )
    assert "token_budget" in result.stop_reason


def test_agent_handles_fetch_error():
    fetcher = _FakeFetcher({})
    llm = _FakeLLM([])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team", llm_client=llm, fetcher=fetcher
        )
    )
    assert "fetch_failed" in result.stop_reason


def test_agent_handles_llm_output_error():
    pages = {"https://acme.test/team": "<p>x</p>"}
    fetcher = _FakeFetcher(pages)
    from leadforge.llm import LLMOutputError
    llm = _FakeLLM([LLMOutputError("bad json after repair")])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team", llm_client=llm, fetcher=fetcher
        )
    )
    assert "llm_output_error" in result.stop_reason


def test_agent_handles_llm_not_configured():
    pages = {"https://acme.test/team": "<p>x</p>"}
    fetcher = _FakeFetcher(pages)
    from leadforge.llm import LLMNotConfigured
    llm = _FakeLLM([LLMNotConfigured("no api key")])
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team", llm_client=llm, fetcher=fetcher
        )
    )
    assert "llm_not_configured" in result.stop_reason


def test_agent_greedy_fallback_without_llm():
    pages = {
        "https://acme.test/team": (
            '<a href="/team/eng">Eng</a>'
            "<h2>Jane Smith</h2><p>Head of Talent</p>"
        ),
        "https://acme.test/team/eng": "<h2>Raj Kumar</h2><p>Engineering Manager</p>",
    }
    fetcher = _FakeFetcher(pages)
    result = asyncio.run(
        find_people_agentic(
            "Acme", "https://acme.test/team", llm_client=False, fetcher=fetcher
        )
    )
    names = {p.name for p in result.people}
    assert "Jane Smith" in names
    assert "Raj Kumar" in names


def test_agent_require_llm_without_client_raises():
    with pytest.raises(ValueError):
        asyncio.run(
            find_people_agentic(
                "Acme", "https://x.test", llm_client=None, require_llm=True
            )
        )


def test_agent_result_shape():
    result = AgentRunResult(company="X")
    assert result.people == []
    assert result.pages == []
    assert result.tokens_used == 0