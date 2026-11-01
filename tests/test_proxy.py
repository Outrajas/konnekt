"""Proxy parsing and per-tier wiring."""

from __future__ import annotations

import pytest

from leadforge.config import Settings
from leadforge.web import proxy as proxy_mod
from leadforge.web.proxy import ProxyError, parse_proxy, redact
from leadforge.web.tiers.http import _proxy_for
from leadforge.web.tiers.obscura import build_argv


def test_parse_http_proxy_with_credentials() -> None:
    config = parse_proxy("http://user:p%40ss@gate.example.net:8080")
    assert config is not None
    assert config.server == "http://gate.example.net:8080"
    assert config.username == "user"
    assert config.password == "p@ss"
    assert config.inline_url() == "http://user:p%40ss@gate.example.net:8080"


def test_parse_socks5_proxy_without_port_uses_1080() -> None:
    config = parse_proxy("socks5://user:pass@gate.example.net")
    assert config is not None
    assert config.port == 1080
    assert config.playwright() == {
        "server": "socks5://gate.example.net:1080",
        "username": "user",
        "password": "pass",
    }


def test_parse_bare_host_port_defaults_to_http() -> None:
    config = parse_proxy("gate.example.net:3128")
    assert config is not None
    assert config.server == "http://gate.example.net:3128"


def test_empty_proxy_means_none() -> None:
    assert parse_proxy(None) is None
    assert parse_proxy("   ") is None


def test_unsupported_scheme_raises() -> None:
    with pytest.raises(ProxyError):
        parse_proxy("ftp://gate.example.net:21")


def test_redact_never_leaks_credentials() -> None:
    assert redact("http://user:secret@gate.example.net:8080") == "http://gate.example.net:8080"
    assert "secret" not in redact("http://user:secret@gate.example.net:8080")
    assert redact(None) == "(none)"


def test_curl_cffi_shape_keeps_credentials_split() -> None:
    config = parse_proxy("http://user:pass@gate.example.net:8080")
    assert config is not None
    assert config.curl_cffi() == {
        "server": "http://gate.example.net:8080",
        "username": "user",
        "password": "pass",
    }


def test_tier1_uses_proxy_when_enabled() -> None:
    settings = Settings(proxy_url="http://user:pass@gate.example.net:8080")
    config = _proxy_for(settings)
    assert config is not None and config.host == "gate.example.net"


def test_tier1_proxy_can_be_disabled() -> None:
    settings = Settings(proxy_url="http://user:pass@gate.example.net:8080", proxy_tier1=False)
    assert _proxy_for(settings) is None


def test_tier1_without_proxy_is_none() -> None:
    assert _proxy_for(Settings()) is None


def test_obscura_argv_carries_proxy_only_when_given() -> None:
    base = build_argv("obscura.exe", "https://example.test", 20, stealth=True)
    assert "--proxy" not in base
    with_proxy = build_argv(
        "obscura.exe", "https://example.test", 20, stealth=False,
        proxy="http://user:pass@gate.example.net:8080",
    )
    assert with_proxy[:2] == ["obscura.exe", "fetch"]
    assert with_proxy[with_proxy.index("--proxy") + 1] == (
        "http://user:pass@gate.example.net:8080"
    )


async def test_browser_context_options_include_split_proxy(monkeypatch) -> None:
    """Tier 3 must hand Playwright server/username/password, not one URL."""
    from leadforge.web.tiers import browser as browser_tier
    from tests.test_browser_tier import FakeContext, FakeEngine, FakePage

    page = FakePage("<html>ok</html>", "https://example.test/")
    context = FakeContext(page)
    engine = FakeEngine(context)
    monkeypatch.setattr(browser_tier, "_load_engine", lambda name: engine)
    browser_tier._SESSION = None
    try:
        await browser_tier.fetch(
            "https://example.test/",
            settings=Settings(
                browser_enabled=True,
                browser_headed=False,
                browser_settle_ms=0,
                proxy_url="socks5://user:pass@gate.example.net:1080",
            ),
            rule=__import__("leadforge.web.policy", fromlist=["DomainRule"]).DomainRule(
                max_tier=3, allow_browser=True
            ),
        )
    finally:
        browser_tier._SESSION = None

    proxy = engine.chromium.options["proxy"]
    assert proxy == {
        "server": "socks5://gate.example.net:1080",
        "username": "user",
        "password": "pass",
    }


def test_browser_proxy_disabled_keeps_options_clean() -> None:
    from leadforge.web.tiers.browser import BrowserSession

    session = BrowserSession(Settings(browser_enabled=True, proxy_tier3=False))
    assert "proxy" not in session._context_options(None)


def test_proxy_module_exports_expected_symbols() -> None:
    for name in ("ProxyConfig", "parse_proxy", "redact", "ProxyError"):
        assert hasattr(proxy_mod, name)
