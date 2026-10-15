import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from leadforge import clock
from leadforge.web import FetchResult
from leadforge.web.tiers.http import RawResponse

log = logging.getLogger(__name__)


class RobotsChecker:
    def __init__(self, user_agent: str, timeout: float = 5.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, tuple[RobotFileParser | None, int | None]] = {}
        self._cache_expires: dict[str, float] = {}
        self.crawl_delays: dict[str, float] = {}
        self.request_fn: Callable[[str], Awaitable[RawResponse]] | None = None
        self.before_request: Callable[[str], Awaitable[None]] | None = None
        self.cache_get: Callable[[str], FetchResult | None] | None = None
        self.cache_put: Callable[..., None] | None = None

    async def allowed(self, url: str, user_agent: str | None = None) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache or time.monotonic() >= self._cache_expires.get(origin, 0):
            robots_url = origin + "/robots.txt"
            cached = self.cache_get(robots_url) if self.cache_get is not None else None
            if cached is not None:
                status_code, body = cached.status or 200, cached.html
            else:
                try:
                    if self.before_request is not None:
                        await self.before_request(parts.hostname or "")
                    if self.request_fn is not None:
                        response = await self.request_fn(robots_url)
                        status_code, body = response.status, response.body
                    else:
                        async with httpx.AsyncClient(
                            timeout=self.timeout, follow_redirects=True
                        ) as client:
                            response = await client.get(robots_url)
                        status_code, body = response.status_code, response.text
                except (httpx.HTTPError, OSError, TimeoutError) as exc:
                    log.warning("Robots unavailable for %s: %s", parts.hostname, exc)
                    self._cache[origin] = (None, None)
                    self._cache_expires[origin] = time.monotonic() + 24 * 60 * 60
                    status_code, body = 0, ""
                if status_code and status_code < 500 and self.cache_put is not None:
                    self.cache_put(
                        FetchResult(
                            robots_url, robots_url, status_code, body, "", 1, False,
                            status_code >= 400, clock.utcnow_iso(),
                        ),
                        ttl_hours=24,
                    )
            if origin not in self._cache or time.monotonic() >= self._cache_expires.get(origin, 0):
                if status_code >= 500:
                    self._cache[origin] = (None, status_code)
                    self._cache_expires[origin] = time.monotonic()
                elif status_code >= 400 or status_code == 0:
                    self._cache[origin] = (None, status_code)
                    self._cache_expires[origin] = time.monotonic() + 24 * 60 * 60
                else:
                    parser = RobotFileParser(robots_url)
                    parser.parse(body.splitlines())
                    delay = parser.crawl_delay(user_agent or self.user_agent)
                    if delay is not None:
                        self.crawl_delays[parts.hostname or ""] = float(delay)
                    self._cache[origin] = (parser, status_code)
                    self._cache_expires[origin] = time.monotonic() + 24 * 60 * 60
        parser, status = self._cache[origin]
        if status is not None and status >= 500:
            return False
        return parser.can_fetch(user_agent or self.user_agent, url) if parser else True
