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
    def __init__(self, context: FakeContext) -> None:
        self.__name__ = "fake_engine"
        self._context = context
        self.chromium = self

    async def launch_persistent_context(self, **options):
        self._context.options = options
        return self._context


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
    monkeypatch.setattr(browser_tier, "_load_engine", lambda preferred="auto": FakeEngine(context))

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
    assert context.options["headless"] is False


async def test_headless_setting_is_respected(monkeypatch) -> None:
    page = FakePage("<html>ok</html>", "https://example.test/")
    context = FakeContext(page)
    monkeypatch.setattr(browser_tier, "_load_engine", lambda preferred="auto": FakeEngine(context))

    await browser_tier.fetch(
        "https://example.test/",
        settings=_settings(browser_headed=False),
        rule=DomainRule(max_tier=3, allow_browser=True),
    )
    assert context.options["headless"] is True


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
