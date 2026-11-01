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

import itertools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")

log = logging.getLogger(__name__)
HOSTNAME = re.compile(r"^[A-Za-z0-9._-]+$")


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
    if not HOSTNAME.fullmatch(parts.hostname):
        raise ProxyError(f"Proxy hostname is not valid: {parts.hostname!r}")
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

class ProxyPool:
    """Round-robin proxy list with a short memory of the ones that just failed.

    Free proxies are unreliable by nature, so the pool is built to tolerate
    that: a proxy that fails a fetch is parked for ``cooldown`` selections
    before being tried again, and the pool never raises when empty — a fetch
    with no healthy proxy simply runs direct (or fails, if one was configured,
    because there is no silent fallback at the transport layer).
    """

    def __init__(self, proxies: list[ProxyConfig], *, cooldown: int = 5) -> None:
        self.proxies = proxies
        self.cooldown = max(1, cooldown)
        self._cycle = itertools.cycle(range(len(proxies))) if proxies else None
        self._parked: dict[str, int] = {}
        self.failures: dict[str, int] = {}

    @classmethod
    def load(cls, path: Path | None, inline: str | None = None) -> ProxyPool | None:
        """Build a pool from a file of URLs, falling back to one inline URL."""
        entries: list[ProxyConfig] = []
        invalid = 0
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                try:
                    parsed = parse_proxy(line)
                except ProxyError:
                    invalid += 1
                    continue
                if parsed is not None:
                    entries.append(parsed)
            log.info("proxy pool: %d usable, %d invalid from %s", len(entries), invalid, path)
        if not entries:
            try:
                single = parse_proxy(inline)
            except ProxyError:
                raise
            if single is not None:
                entries = [single]
        return cls(entries) if entries else None

    def __len__(self) -> int:
        return len(self.proxies)

    def next(self) -> ProxyConfig | None:
        """Next healthy proxy, or None when the pool is empty/exhausted."""
        if not self.proxies or self._cycle is None:
            return None
        for _ in range(len(self.proxies)):
            index = next(self._cycle)
            proxy = self.proxies[index]
            if self._parked.get(proxy.server, 0) > 0:
                self._parked[proxy.server] -= 1
                continue
            return proxy
        return self.proxies[0]

    def mark_bad(self, proxy: ProxyConfig | None) -> None:
        if proxy is None:
            return
        self.failures[proxy.server] = self.failures.get(proxy.server, 0) + 1
        self._parked[proxy.server] = self.cooldown

    def health(self) -> list[tuple[str, int]]:
        """(server, failure count) sorted worst-first, safe to print."""
        return sorted(self.failures.items(), key=lambda kv: -kv[1])
