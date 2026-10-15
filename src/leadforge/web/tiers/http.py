import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from leadforge.config import Settings

MAX_RESPONSE_BYTES = 3 * 1024 * 1024


@dataclass
class RawResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: str


def tier1_backend() -> str:
    try:
        from scrapling.fetchers import Fetcher  # noqa: F401
    except ImportError:
        return "httpx"
    return "scrapling"


async def fetch(
    url: str, settings: Settings | None = None, *, use_httpx: bool = False
) -> RawResponse:
    settings = settings or Settings()
    headers = {
        "User-Agent": settings.user_agent,
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = httpx.Timeout(settings.http_timeout_seconds)
    fetcher_class: Any = None
    if not use_httpx:
        try:
            from scrapling.fetchers import Fetcher as scrapling_fetcher
        except ImportError:
            pass
        else:
            fetcher_class = scrapling_fetcher
    if fetcher_class is not None:
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
        )
        body = str(response.text)
        if len(body.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeded 3 MB limit")
        return RawResponse(str(response.url), int(response.status), dict(response.headers), body)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
        headers=headers,
    ) as client:
        async with client.stream("GET", url) as response:
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ValueError("Response exceeded 3 MB limit")
            body = bytes(chunks).decode(response.encoding or "utf-8", errors="replace")
            return RawResponse(
                str(response.url), response.status_code, dict(response.headers), body
            )
