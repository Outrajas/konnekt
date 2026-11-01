import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from leadforge.config import Settings
from leadforge.web import proxy as proxy_mod

MAX_RESPONSE_BYTES = 3 * 1024 * 1024


@dataclass
class RawResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: str
    backend: str = "unknown"


def tier1_backend() -> str:
    try:
        from scrapling.fetchers import Fetcher  # noqa: F401
    except ImportError:
        return "httpx"
    return "scrapling"


def _proxy_for(settings: Settings) -> proxy_mod.ProxyConfig | None:
    """Resolve the Tier-1 proxy, or None when unset/disabled."""
    if not settings.proxy_tier1:
        return None
    raw = settings.proxy_url.get_secret_value() if settings.proxy_url else None
    return proxy_mod.parse_proxy(raw)


async def fetch(
    url: str,
    settings: Settings | None = None,
    *,
    use_httpx: bool = False,
    proxy: proxy_mod.ProxyConfig | None = None,
) -> RawResponse:
    settings = settings or Settings()
    headers = {
        "User-Agent": settings.user_agent,
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = httpx.Timeout(settings.http_timeout_seconds)
    proxy_config = proxy or _proxy_for(settings)
    fetcher_class: Any = None
    if not use_httpx:
        try:
            from scrapling.fetchers import Fetcher as scrapling_fetcher
        except ImportError:
            pass
        else:
            fetcher_class = scrapling_fetcher
    if fetcher_class is not None:
        extra: dict[str, Any] = {}
        if proxy_config is not None:
            extra["proxy"] = proxy_config.inline_url()
        response = await asyncio.to_thread(
            fetcher_class.get,
            url,
            impersonate="chrome131",
            timeout=settings.http_timeout_seconds,
            headers=headers,
            follow_redirects=True,
            max_redirects=5,
            retries=0,
            cookies=None,
            auth=None,
            **extra,
        )
        body = str(response.text)
        if len(body.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeded 3 MB limit")
        return RawResponse(
            str(response.url), int(response.status), dict(response.headers), body, "scrapling"
        )
    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": True,
        "max_redirects": 5,
        "headers": headers,
    }
    if proxy_config is not None:
        client_kwargs["proxy"] = proxy_config.inline_url()
    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream("GET", url) as response:
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ValueError("Response exceeded 3 MB limit")
            body = bytes(chunks).decode(response.encoding or "utf-8", errors="replace")
            return RawResponse(
                str(response.url), response.status_code, dict(response.headers), body, "httpx"
            )
