import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response

from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.web import FetchResult
from leadforge.web.budget import Budget
from leadforge.web.cache import PageCache, normalize_url
from leadforge.web.denylist import check_host
from leadforge.web.detect import classify_response
from leadforge.web.errors import BudgetExhausted, DeniedHost, DomainPaused
from leadforge.web.policy import DomainPolicy, DomainRule
from leadforge.web.robots import RobotsChecker
from leadforge.web.tiers.obscura import build_argv


def test_policy_exact_subdomain_fallback_and_bad_file(tmp_path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text('[default]\nenabled=true\nmax_tier=2\n["linkedin.com"]\n'
                    'enabled=true\nmax_tier=1\n', encoding="utf-8")
    policy = DomainPolicy.load(path)
    assert policy.resolve("linkedin.com").max_tier == 1
    assert policy.resolve("jobs.linkedin.com").max_tier == 1
    assert policy.resolve("example.com").max_tier == 2
    path.write_text("not = [toml", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid domain policy"):
        DomainPolicy.load(path)


@pytest.mark.parametrize(
    ("status", "headers", "html", "expected"),
    [
        (200, {}, "<p>hello</p>", "ok"),
        (403, {}, "no", "blocked"),
        (403, {}, "x" * 3000, "blocked"),
        (200, {"cf-mitigated": "challenge"}, "", "challenge"),
        (429, {}, "", "rate_limited"),
        (503, {"Retry-After": "30"}, "", "rate_limited"),
        (
            200,
            {},
            '<div id="__next"></div><script></script><script></script><script></script>',
            "thin",
        ),
    ],
)
def test_response_classification(status, headers, html, expected) -> None:
    assert classify_response(status, headers, html) == expected


def test_denylist_matches_subdomains() -> None:
    with pytest.raises(DeniedHost):
        check_host("m.facebook.com", {"facebook.com"})


def test_cache_normalization_and_hit(tmp_path) -> None:
    db_path = tmp_path / "cache.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    settings = Settings(
        _env_file=None, db_path=db_path, cache_dir=tmp_path / "cache"
    )
    cache = PageCache(settings)
    assert normalize_url("HTTPS://Example.com/a?utm_source=x&b=2#frag") == (
        "https://example.com/a?b=2"
    )
    result = FetchResult(
        "https://example.com/a?b=2", "https://example.com/a?b=2", 200,
        "<p>public</p>", "", 1, False, False, "2026-10-15T00:00:00+00:00",
    )
    assert cache.get(result.url) is None
    cache.put(result)
    cached = cache.get("https://EXAMPLE.com/a?b=2#section")
    assert cached is not None and cached.from_cache
    assert cached.html == result.html


@pytest.mark.asyncio
async def test_budget_cap_pause_and_day_reset(tmp_path) -> None:
    db_path = tmp_path / "budget.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    now = [datetime(2026, 10, 15, tzinfo=UTC)]
    async def sleep(_seconds: float) -> None:
        now[0] += timedelta(seconds=_seconds)
    budget = Budget(str(db_path), clock=lambda: now[0], sleep=sleep, uniform=lambda _a, _b: 1)
    rule = DomainRule(min_delay_s=1, max_delay_s=1, daily_cap=1)
    await budget.acquire("example.test", rule)
    with pytest.raises(BudgetExhausted):
        await budget.acquire("example.test", rule)
    await budget.pause("example.test", 48, "challenge")
    now[0] += timedelta(hours=24)
    with pytest.raises(DomainPaused):
        await budget.acquire("example.test", rule)
    now[0] += timedelta(hours=25)
    await budget.acquire("example.test", rule)


@pytest.mark.asyncio
async def test_budget_spacing_and_independent_host_locks(tmp_path) -> None:
    db_path = tmp_path / "spacing.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    now = [datetime(2026, 10, 15, tzinfo=UTC)]
    first_sleep_started = asyncio.Event()
    release_first = asyncio.Event()

    async def sleep(seconds: float) -> None:
        first_sleep_started.set()
        await release_first.wait()
        now[0] += timedelta(seconds=seconds)

    budget = Budget(str(db_path), clock=lambda: now[0], sleep=sleep, uniform=lambda a, b: b)
    rule = DomainRule(min_delay_s=2, max_delay_s=4, daily_cap=5)
    await budget.acquire("one.example", rule)
    pending = asyncio.create_task(budget.acquire("one.example", rule))
    await first_sleep_started.wait()
    await budget.acquire("two.example", rule)
    assert not pending.done()
    release_first.set()
    await pending
    assert now[0] == datetime(2026, 10, 15, 0, 0, 4, tzinfo=UTC)


def test_default_policy_never_allows_tier_three() -> None:
    from leadforge.config import settings
    policy = DomainPolicy.load(settings.domain_policy_path)
    assert policy.default.max_tier < 3
    assert not policy.default.allow_challenge_solving
    assert policy.budget_key("jobs.linkedin.com") == "linkedin.com"
    for platform in ("linkedin.com", "indeed.com", "glassdoor.com"):
        rule = policy.resolve(platform)
        assert rule.enabled and rule.max_tier == 1
        assert not rule.allow_challenge_solving
        assert rule.daily_cap == 10


def test_obscura_argv_is_a_list_and_tier_three_stays_disabled() -> None:
    from leadforge.web.errors import TierUnavailable
    from leadforge.web.tiers.stealth import require_allowed
    assert build_argv("obscura.exe", "https://example.test", 20, stealth=False) == [
        "obscura.exe", "fetch", "https://example.test", "--dump", "html",
        "--timeout", "20", "--quiet",
    ]
    assert build_argv("obscura.exe", "https://example.test", 20, stealth=True)[-1] == "--stealth"
    with pytest.raises(TierUnavailable, match="disabled by domain policy"):
        require_allowed(DomainRule(max_tier=2, allow_challenge_solving=False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "path", "expected"),
    [
        (200, "User-agent: *\nAllow: /\n", "/public", True),
        (200, "User-agent: *\nDisallow: /private\n", "/private/x", False),
        (404, "", "/anything", True),
        (500, "", "/anything", False),
    ],
)
async def test_robots_status_and_path(status, body, path, expected) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://example.test/robots.txt").mock(
            return_value=Response(status, text=body)
        )
        checker = RobotsChecker("LeadForgeTestBot")
        assert await checker.allowed(f"https://example.test{path}") is expected


@pytest.mark.asyncio
async def test_robots_crawl_delay_is_recorded() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://example.test/robots.txt").mock(
            return_value=Response(
                200, text="User-agent: LeadForgeTestBot\nCrawl-delay: 12\nAllow: /\n"
            )
        )
        checker = RobotsChecker("LeadForgeTestBot")
        assert await checker.allowed("https://example.test/")
        assert checker.crawl_delays["example.test"] == 12


@pytest.mark.asyncio
async def test_router_challenge_pauses_without_escalation(tmp_path, monkeypatch) -> None:
    from leadforge.web import fetcher as fetcher_module
    from leadforge.web.errors import Blocked
    from leadforge.web.tiers.http import RawResponse

    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        '[default]\nenabled=true\nmin_delay_s=0\nmax_delay_s=0\ndaily_cap=10\n'
        'max_tier=2\nobey_robots=false\n', encoding="utf-8"
    )
    db_path = tmp_path / "router.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    settings = Settings(
        _env_file=None, db_path=db_path, cache_dir=tmp_path / "cache",
        domain_policy_path=policy_path, denylist_hosts_path=tmp_path / "denylist.txt",
        pause_hours_on_block=48,
    )
    seen_tiers: list[int] = []

    async def fake_http(url, settings):
        seen_tiers.append(1)
        return RawResponse(url, 200, {"cf-mitigated": "challenge"}, "<p>Checking browser</p>")

    monkeypatch.setattr(fetcher_module.http, "fetch", fake_http)
    router = fetcher_module.Fetcher(settings)
    with pytest.raises(Blocked):
        await router.fetch("https://example.test/", use_cache=False)
    assert seen_tiers == [1]
    with pytest.raises(DomainPaused):
        await router.fetch("https://example.test/", use_cache=False)


@pytest.mark.asyncio
async def test_disabled_host_is_refused_before_any_request(tmp_path, monkeypatch) -> None:
    from leadforge.web import fetcher as fetcher_module
    from leadforge.web.errors import DomainPaused

    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        '[default]\nenabled=true\nmax_tier=1\n["blocked.test"]\nenabled=false\n',
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None, db_path=tmp_path / "refuse.db", cache_dir=tmp_path / "cache",
        domain_policy_path=policy_path, denylist_hosts_path=tmp_path / "denylist.txt",
    )

    async def unexpected(url, settings):
        raise AssertionError("disabled host must be refused before the network")

    monkeypatch.setattr(fetcher_module.http, "fetch", unexpected)
    with pytest.raises(DomainPaused, match="disabled by policy"):
        await fetcher_module.Fetcher(settings).fetch("https://blocked.test/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "purpose", "allowed"),
    [
        ("linkedin.com", "", False),
        ("linkedin.com", "search-snippet-miss", True),
        ("indeed.com", "person-search", False),
        ("indeed.com", "job", True),
        ("glassdoor.com", "hiring-signal", True),
    ],
)
async def test_platform_fetch_purpose_guards(
    tmp_path, monkeypatch, platform, purpose, allowed
) -> None:
    from leadforge.web import fetcher as fetcher_module
    from leadforge.web.errors import DomainPaused
    from leadforge.web.tiers.http import RawResponse

    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        '[default]\nenabled=true\nmin_delay_s=0\nmax_delay_s=0\nmax_tier=2\n'
        'obey_robots=false\n' + ''.join(
            f'["{domain}"]\nenabled=true\nmin_delay_s=0\nmax_delay_s=0\n'
            'daily_cap=10\nmax_tier=1\nobey_robots=false\n'
            'allow_challenge_solving=false\n'
            for domain in ("linkedin.com", "indeed.com", "glassdoor.com")
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / f"{platform}.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    settings = Settings(
        _env_file=None, db_path=db_path, cache_dir=tmp_path / f"cache-{platform}",
        domain_policy_path=policy_path, denylist_hosts_path=tmp_path / "none.txt",
    )
    calls: list[str] = []

    async def fake_http(url, settings):
        calls.append(url)
        return RawResponse(url, 200, {}, "<p>ok</p>")

    monkeypatch.setattr(fetcher_module.http, "fetch", fake_http)
    router = fetcher_module.Fetcher(settings)
    if not allowed:
        with pytest.raises(DomainPaused):
            await router.fetch(f"https://www.{platform}/", purpose=purpose, use_cache=False)
        assert not calls
    else:
        result = await router.fetch(
            f"https://www.{platform}/", purpose=purpose, use_cache=False
        )
        assert result.tier == 1
        assert calls
