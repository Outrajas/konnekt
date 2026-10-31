"""Tier-3 (headed browser) gating and navigation, without launching a browser."""

from __future__ import annotations

import pytest

from leadforge.config import Settings
from leadforge.web.errors import TierUnavailable
from leadforge.web.policy import DomainRule
from leadforge.web.tiers import browser as browser_tier
from leadforge.web.tiers.http import RawResponse


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class FakePage:
    def __init__(self, html: str, url: str, status: int = 200) -> None:
        self._html = html
        self.url = url
        self.goto_calls: list[tuple[str, dict]] = []
        self.closed = False
        self._status = status

    async def goto(self, url: str, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url
        return FakeResponse(self._status)

    async def content(self) -> str:
        return self._html

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False
        self.options: dict | None = None

    async def new_page(self) -> FakePage:
        return self.page

    def set_default_timeout(self, _ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeEngine:
    """Stands in for ``patchright.async_api`` / ``playwright.async_api``."""

    def __init__(self, context: FakeContext) -> None:
        self.__name__ = "fake_engine.async_api"
        self._context = context
        self.chromium = FakeChromium(context)

    def async_playwright(self) -> FakeStarter:
        self.playwright = FakePlaywright(self.chromium)
        return FakeStarter(self.playwright)


class FakeChromium:
    def __init__(self, context: FakeContext) -> None:
        self._context = context
        self.options: dict | None = None

    async def launch_persistent_context(self, **options):
        self.options = options
        return self._context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self._pw = playwright

    async def start(self) -> FakePlaywright:
        return self._pw


@pytest.fixture(autouse=True)
def _reset_session(monkeypatch):
    browser_tier._SESSION = None
    yield
    browser_tier._SESSION = None


def _settings(tmp_path=None, **overrides) -> Settings:
    values = {
        "browser_enabled": True,
        "browser_headed": True,
        "browser_settle_ms": 0,
        "browser_manual_challenge_wait_s": 0,
    }
    values.update(overrides)
    return Settings(**values)


def test_browser_defaults_are_off_and_headed() -> None:
    settings = Settings()
    assert settings.browser_enabled is False
    assert settings.browser_headed is True
    assert settings.browser_max_pages_per_session >= 1


def test_policy_default_forbids_tier_three() -> None:
    assert DomainRule().allow_browser is False
    assert DomainRule().max_tier < 3


async def test_tier_three_refused_when_browser_disabled() -> None:
    rule = DomainRule(max_tier=3, allow_browser=True)
    with pytest.raises(TierUnavailable):
        await browser_tier.fetch("https://example.test/x", settings=_settings(
            browser_enabled=False), rule=rule)


async def test_tier_three_refused_without_domain_permission() -> None:
    with pytest.raises(TierUnavailable):
        await browser_tier.fetch(
            "https://example.test/x", settings=_settings(), rule=DomainRule(max_tier=3)
        )


async def test_tier_three_refused_above_domain_max_tier() -> None:
    with pytest.raises(TierUnavailable):
        await browser_tier.fetch(
            "https://example.test/x",
            settings=_settings(),
            rule=DomainRule(max_tier=2, allow_browser=True),
        )


def test_challenge_detection() -> None:
    assert browser_tier.looks_like_challenge("<html>Just a moment...</html>")
    assert browser_tier.looks_like_challenge("Please verify you are human")
    assert not browser_tier.looks_like_challenge("<html><h1>Careers</h1></html>")


async def test_navigate_returns_raw_response(monkeypatch) -> None:
    page = FakePage("<html><h1>Careers</h1></html>", "https://example.test/careers")
    context = FakeContext(page)
    engine = FakeEngine(context)
    monkeypatch.setattr(browser_tier, "_load_engine", lambda preferred="auto": engine)

    result = await browser_tier.fetch(
        "https://example.test/careers",
        settings=_settings(),
        rule=DomainRule(max_tier=3, allow_browser=True),
    )

    assert isinstance(result, RawResponse)
    assert result.backend == "browser"
    assert result.status == 200
    assert result.url == "https://example.test/careers"
    assert "Careers" in result.body
    assert page.closed is True
    assert engine.chromium.options["headless"] is False


async def test_headless_setting_is_respected(monkeypatch) -> None:
    page = FakePage("<html>ok</html>", "https://example.test/")
    context = FakeContext(page)
    engine = FakeEngine(context)
    monkeypatch.setattr(browser_tier, "_load_engine", lambda preferred="auto": engine)

    await browser_tier.fetch(
        "https://example.test/",
        settings=_settings(browser_headed=False),
        rule=DomainRule(max_tier=3, allow_browser=True),
    )
    assert engine.chromium.options["headless"] is True


async def test_challenge_page_fails_after_bounded_wait(monkeypatch) -> None:
    page = FakePage("<html>Just a moment...</html>", "https://example.test/")
    context = FakeContext(page)
    monkeypatch.setattr(browser_tier, "_load_engine", lambda preferred="auto": FakeEngine(context))

    from leadforge.web.errors import FetchFailed

    with pytest.raises(FetchFailed, match="Challenge"):
        await browser_tier.fetch(
            "https://example.test/",
            settings=_settings(browser_manual_challenge_wait_s=0),
            rule=DomainRule(max_tier=3, allow_browser=True),
        )


async def test_engine_ladder_falls_back_when_patchright_is_unusable(monkeypatch) -> None:
    """A Patchright install without its browser build must not break the tier."""

    page = FakePage("<html><h1>Careers</h1></html>", "https://example.test/careers")
    context = FakeContext(page)
    engine = FakeEngine(context)
    attempted: list[str] = []

    def fake_load(name: str):
        attempted.append(name)
        if name == "patchright":
            raise ImportError("patchright: browser build missing")
        return engine

    monkeypatch.setattr(browser_tier, "_load_engine", fake_load)
    result = await browser_tier.fetch(
        "https://example.test/careers",
        settings=_settings(),
        rule=DomainRule(max_tier=3, allow_browser=True),
    )
    assert result.status == 200
    assert attempted == ["patchright", "playwright"]


def test_engine_status_reports_importable_engines(monkeypatch) -> None:
    def fake_load(name: str):
        if name == "patchright":
            raise ImportError("nope")
        return object()

    monkeypatch.setattr(browser_tier, "_load_engine", fake_load)
    status = browser_tier.engine_status("auto")
    assert status["available"] is True
    assert status["engine"] == "playwright"


async def test_router_skips_unavailable_tier_and_climbs(monkeypatch, tmp_path) -> None:
    """Tier 2 missing (no Obscura) must not stop the climb to Tier 3."""
    from leadforge.web.fetcher import Fetcher
    from leadforge.web.tiers.http import RawResponse

    settings = Settings(
        browser_enabled=True,
        db_path=tmp_path / "lf.db",
        cache_dir=tmp_path / "cache",
        domain_policy_path=tmp_path / "policy.toml",
        denylist_hosts_path=tmp_path / "deny.txt",
    )
    (tmp_path / "cache").mkdir()
    (tmp_path / "deny.txt").write_text("", encoding="utf-8")
    (tmp_path / "policy.toml").write_text(
        '["default"]\nmin_delay_s = 0\nmax_delay_s = 0\ndaily_cap = 100\n'
        "max_tier = 3\nobey_robots = false\nallow_browser = true\nenabled = true\n",
        encoding="utf-8",
    )
    fetcher = Fetcher(settings)
    from leadforge.db import connect, init_schema

    conn = connect(settings.db_path)
    init_schema(conn)
    conn.close()
    tiers: list[int] = []

    async def fake_acquire(*_args, **_kwargs):
        return None

    async def fake_tier(tier, url, rule, *, force_httpx=False):
        tiers.append(tier)
        if tier == 2:
            raise __import__("leadforge.web.errors", fromlist=["TierUnavailable"]).TierUnavailable(
                "no obscura"
            )
        return RawResponse(url, 200, {}, "<html><body>ok</body></html>", "browser")

    async def fake_allowed(_url):
        return True

    monkeypatch.setattr(fetcher.budget, "acquire", fake_acquire)
    monkeypatch.setattr(fetcher.robots, "allowed", fake_allowed)
    monkeypatch.setattr(fetcher, "_fetch_tier", fake_tier)
    monkeypatch.setattr(fetcher, "_log_fetch", lambda *a, **k: None)

    result = await fetcher.fetch("https://example.test/x", min_tier=2, max_tier=3, purpose="person")
    assert tiers == [2, 3]
    assert result.tier == 3
    assert result.status == 200

