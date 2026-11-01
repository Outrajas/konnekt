"""Proxy plumbing shared by every fetch tier.

One setting (``LEADFORGE_PROXY_URL``) has to serve three very different
transports, each of which wants the credentials in a different shape:

* ``httpx`` wants one URL with the userinfo inline.
* ``curl_cffi`` (inside Scrapling's Fetcher) wants the same inline URL.
* Playwright wants ``{"server": ..., "username": ..., "password": ...}`` split.

The rules here are boring on purpose: parse once, never log the password, and
fail *loudly at parse time* rather than silently scraping from the home IP.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


class ProxyError(ValueError):
    """Raised when a configured proxy URL cannot be used."""


@dataclass(frozen=True)
class ProxyConfig:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def server(self) -> str:
        """``scheme://host:port`` — the part safe to log and show."""
        return f"{self.scheme}://{self.host}:{self.port}"

    def label(self) -> str:
        return self.server

    def inline_url(self) -> str:
        """URL with userinfo, for httpx and curl_cffi."""
        if self.username is None:
            return self.server
        userinfo = quote(self.username, safe="")
        if self.password is not None:
            userinfo += ":" + quote(self.password, safe="")
        return f"{self.scheme}://{userinfo}@{self.host}:{self.port}"

    def playwright(self) -> dict[str, str]:
        """Playwright's proxy shape (credentials split out)."""
        proxy = {"server": self.server}
        if self.username is not None:
            proxy["username"] = self.username
        if self.password is not None:
            proxy["password"] = self.password
        return proxy

    def curl_cffi(self) -> dict[str, str]:
        """curl_cffi accepts either a URL string or this dict."""
        proxy = {"server": self.server}
        if self.username is not None:
            proxy["username"] = self.username
            proxy["password"] = self.password or ""
        return proxy


def parse_proxy(raw: str | None) -> ProxyConfig | None:
    """Parse ``scheme://user:pass@host:port``; ``None``/empty means no proxy."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    parts = urlsplit(value if "://" in value else f"http://{value}")
    scheme = parts.scheme.lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise ProxyError(
            f"Unsupported proxy scheme {scheme!r}; use one of {', '.join(SUPPORTED_SCHEMES)}"
        )
    if not parts.hostname:
        raise ProxyError("Proxy URL has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ProxyError(f"Proxy URL has an invalid port: {exc}") from exc
    if port is None:
        port = 1080 if scheme.startswith("socks") else 8080
    username = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else None
    return ProxyConfig(scheme, parts.hostname, int(port), username, password)


def redact(raw: str | None) -> str:
    """Anything derived from a proxy URL that is safe to put in a log line."""
    try:
        config = parse_proxy(raw)
    except ProxyError:
        return "(invalid proxy)"
    return config.label() if config else "(none)"
