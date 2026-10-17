from pathlib import Path

import pytest
from typer.testing import CliRunner

from leadforge.cli import app
from leadforge.llm.client import LLMNotConfigured
from leadforge.llm.schemas import NextLink, PageExtraction, PersonMention
from leadforge.models import (
    Contact,
    EmailCandidate,
    VerificationMethod,
    VerificationVerdict,
)
from leadforge.stages.replay import ReplayFetcher
from leadforge.stages.walk import WalkBudget, WalkPage, walk_person
from leadforge.web.extract import Link

CONTACT = Contact(
    first_name="Jane", last_name="Doe", domain="acme.invalid", title="Engineering Manager"
)


def team_html(*links: tuple[str, str]) -> str:
    anchor_tags = "".join(f'<a href="{url}">{label}</a>' for url, label in links)
    return f"<html><body><p>Jane Doe is an Engineering Manager.</p>{anchor_tags}</body></html>"


def extraction_for(links: list[Link]) -> PageExtraction:
    return PageExtraction(
        page_kind="team",
        people=[PersonMention(name="Jane Doe", professional=True)],
        next_links=[
            NextLink(candidate_id=index, reason="target link", expected="bio")
            for index, _ in enumerate(links, start=1)
        ],
    )


async def noop_verify(candidates: list[EmailCandidate]) -> list[EmailCandidate]:
    return candidates


@pytest.mark.asyncio
async def test_walk_finds_personal_site_email_two_hops_away() -> None:
    pages = {
        "https://acme.invalid/team": team_html(
            ("https://jane.github.io/", "Jane Doe personal site")
        ),
        "https://jane.github.io/": (
            "<html><body><h1>Jane Doe</h1><p>Contact Jane Doe at "
            "jane@acme.invalid about professional work.</p></body></html>"
        ),
    }
    calls: list[str] = []

    async def fetch(url: str) -> WalkPage:
        calls.append(url)
        return WalkPage(url, pages[url])

    async def llm(**kwargs) -> PageExtraction:
        return extraction_for(kwargs["candidate_links"])

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=noop_verify,
        seed_urls_override=["https://acme.invalid/team"],
    )
    assert calls == ["https://acme.invalid/team", "https://jane.github.io/"]
    assert result.status == "found"
    assert result.stop_reason == "email_found"
    assert result.candidates[0].email == "jane@acme.invalid"
    assert result.candidates[0].evidence_tier == "A"


@pytest.mark.asyncio
async def test_walk_stops_at_eight_fetches_exactly() -> None:
    urls = [f"https://acme.invalid/team/{index}" for index in range(10)]
    pages = {
        url: team_html((f"https://acme.invalid/team/{index + 1}", "About"))
        for index, url in enumerate(urls)
    }
    calls: list[str] = []

    async def fetch(url: str) -> WalkPage:
        calls.append(url)
        return WalkPage(url, pages[url])

    async def llm(**kwargs) -> PageExtraction:
        return extraction_for(kwargs["candidate_links"])

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=noop_verify,
        budget=WalkBudget(max_fetches=8, max_depth=20, stale_hops=2, wall_clock_s=600),
        seed_urls_override=[urls[0]],
    )
    assert len(calls) == 8, (result.stop_reason, result.evidence, result.pages_visited)
    assert result.stop_reason == "budget"


@pytest.mark.asyncio
async def test_walk_stops_after_two_consecutive_stale_hops() -> None:
    urls = [f"https://acme.invalid/page/{index}" for index in range(3)]

    async def fetch(url: str) -> WalkPage:
        return WalkPage(url, "<html><body><p>Jane Doe, Engineering Manager.</p></body></html>")

    async def llm(**kwargs) -> PageExtraction:
        return PageExtraction(page_kind="team")

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=noop_verify,
        seed_urls_override=urls,
    )
    assert result.stop_reason == "stale"
    assert len(result.pages_visited) == 2


@pytest.mark.asyncio
async def test_walk_never_follows_denylisted_link_even_if_llm_proposes_it() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> WalkPage:
        calls.append(url)
        return WalkPage(url, team_html(("https://blocked.invalid/jane", "Jane Doe profile")))

    async def llm(**kwargs) -> PageExtraction:
        return PageExtraction(
            page_kind="team",
            next_links=[
                NextLink(url="https://blocked.invalid/jane", reason="profile", expected="bio")
            ],
        )

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=noop_verify,
        seed_urls_override=["https://acme.invalid/team"],
        denylist_hosts={"blocked.invalid"},
    )
    assert calls == ["https://acme.invalid/team"]
    assert result.stop_reason == "no_edges"


@pytest.mark.asyncio
async def test_walk_never_follows_url_not_in_extracted_candidates() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> WalkPage:
        calls.append(url)
        return WalkPage(url, team_html())

    async def llm(**kwargs) -> PageExtraction:
        return PageExtraction(
            page_kind="team",
            next_links=[
                NextLink(url="https://other.github.io/fake", reason="fake", expected="bio")
            ],
        )

    await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=noop_verify,
        seed_urls_override=["https://acme.invalid/team"],
    )
    assert calls == ["https://acme.invalid/team"]


@pytest.mark.asyncio
async def test_walk_ignores_email_on_page_that_fails_authorship_test() -> None:
    pages = {
        "https://acme.invalid/team": team_html(("https://stranger.github.io/", "External profile")),
        "https://stranger.github.io/": (
            "<html><body><p>Unrelated person, jane@acme.invalid</p></body></html>"
        ),
    }
    calls: list[str] = []

    async def fetch(url: str) -> WalkPage:
        calls.append(url)
        return WalkPage(url, pages[url])

    async def llm(**kwargs) -> PageExtraction:
        return extraction_for(kwargs["candidate_links"])

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=noop_verify,
        seed_urls_override=["https://acme.invalid/team"],
    )
    assert calls == ["https://acme.invalid/team", "https://stranger.github.io/"]
    assert result.status == "not_found"
    assert all(candidate.evidence_tier != "A" for candidate in result.candidates)


@pytest.mark.asyncio
async def test_walk_degrades_to_heuristic_order_when_llm_not_configured() -> None:
    pages = {
        "https://acme.invalid/team": team_html(("https://jane.github.io/", "Jane Doe profile")),
        "https://jane.github.io/": (
            "<html><body><p>Jane Doe contact jane@acme.invalid</p></body></html>"
        ),
    }

    async def fetch(url: str) -> WalkPage:
        return WalkPage(url, pages[url])

    async def missing_llm(**kwargs):
        raise LLMNotConfigured("no key")

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=missing_llm,
        verify_fn=noop_verify,
        seed_urls_override=["https://acme.invalid/team"],
    )
    assert result.status == "found"
    assert result.pages_visited[-1] == "https://jane.github.io/"


@pytest.mark.asyncio
async def test_walk_wall_clock_stop_uses_injected_clock() -> None:
    ticks = iter([0.0, 0.0, 600.0])

    async def fetch(url: str) -> WalkPage:
        return WalkPage(url, team_html(("https://jane.github.io/", "Jane Doe profile")))

    async def llm(**kwargs) -> PageExtraction:
        return extraction_for(kwargs["candidate_links"])

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=noop_verify,
        clock=lambda: next(ticks),
        budget=WalkBudget(wall_clock_s=600),
        seed_urls_override=["https://acme.invalid/team"],
    )
    assert result.stop_reason == "time"
    assert len(result.pages_visited) == 1


@pytest.mark.asyncio
async def test_walk_marks_denied_seed_and_persists_stop_state() -> None:
    calls: list[str] = []
    persisted = []

    async def fetch(url: str) -> WalkPage:
        calls.append(url)
        return WalkPage(url, "")

    async def llm(**kwargs) -> PageExtraction:
        return PageExtraction(page_kind="other")

    result = await walk_person(
        person_id=7,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=noop_verify,
        seed_urls_override=["https://denied.invalid/team"],
        denylist_hosts={"denied.invalid"},
        persist_fn=lambda value: persisted.append(value),
    )
    assert result.stop_reason == "denied"
    assert calls == []
    assert persisted == [result]


@pytest.mark.asyncio
async def test_established_pattern_is_verified_before_any_page_fetch() -> None:
    verified_batches: list[list[EmailCandidate]] = []

    async def fetch(url: str) -> WalkPage:
        raise AssertionError("Established pattern should be checked before fetching pages")

    async def llm(**kwargs) -> PageExtraction:
        raise AssertionError("No page should be fetched for a successful free-step candidate")

    async def verify(candidates: list[EmailCandidate]) -> list[EmailCandidate]:
        verified_batches.append(candidates)
        return [
            candidates[0].model_copy(
                update={
                    "verification_method": VerificationMethod.SMTP,
                    "verification_verdict": VerificationVerdict.SEND,
                    "catch_all_domain": False,
                }
            )
        ]

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=verify,
        established_pattern="flast",
    )
    assert result.status == "found"
    assert result.stop_reason == "email_found"
    assert result.candidates[0].evidence_tier == "B"
    assert result.candidates[0].confidence == 0.5
    assert len(verified_batches) == 1


@pytest.mark.asyncio
async def test_fallback_pattern_candidates_are_passed_to_injected_verifier() -> None:
    called = []

    async def fetch(url: str) -> WalkPage:
        return WalkPage(url, "<html><body><p>No matching person here.</p></body></html>")

    async def llm(**kwargs) -> PageExtraction:
        return PageExtraction(page_kind="other")

    async def verify(candidates: list[EmailCandidate]) -> list[EmailCandidate]:
        called.extend(candidates)
        return candidates

    await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetch,
        llm_fn=llm,
        verify_fn=verify,
        seed_urls_override=["https://acme.invalid/team"],
    )
    assert len(called) == 12


@pytest.mark.asyncio
async def test_replay_fetcher_serves_recorded_two_hop_walk_offline() -> None:
    fixture_dir = Path("data/fixtures/replay/person_walk")
    fetcher = ReplayFetcher(fixture_dir)

    async def no_llm(**kwargs):
        raise LLMNotConfigured("replay is deterministic")

    result = await walk_person(
        person_id=1,
        contact=CONTACT,
        fetch_fn=fetcher,
        llm_fn=no_llm,
        verify_fn=noop_verify,
        seed_urls_override=fetcher.seeds,
    )
    assert result.status == "found"
    assert result.stop_reason == "email_found"
    assert result.candidates[0].email == "jane@acme.invalid"
    assert len(result.pages_visited) == 2


def test_replay_manifest_rejects_path_escape(tmp_path) -> None:
    outside = tmp_path / "outside.html"
    outside.write_text("not used", encoding="utf-8")
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "manifest.json").write_text(
        '{"pages":{"https://example.invalid/":"../outside.html"}}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="outside its directory"):
        ReplayFetcher(replay_dir)


def test_walk_cli_replay_mode_runs_offline_and_persists_result(monkeypatch) -> None:
    import leadforge.cli as cli

    persisted = []
    monkeypatch.setattr(
        cli,
        "_persist_walk_result",
        lambda result, **_kwargs: persisted.append(result),
    )
    replay_dir = Path("data/fixtures/replay/person_walk")
    result = CliRunner().invoke(
        app,
        [
            "walk",
            "--company-domain",
            "acme.invalid",
            "--first",
            "Jane",
            "--last",
            "Doe",
            "--title",
            "Engineering Manager",
            "--replay",
            str(replay_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Walk found: stop=email_found" in result.output
    assert persisted[0].candidates[0].email == "jane@acme.invalid"
