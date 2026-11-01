"""Harvest and health-check free HTTP/SOCKS proxies into data/proxies.txt.

Nothing here is magic and nothing here is reliable: public proxy lists are full
of dead, cloned and honeypot entries. The point is to give the router a pool of
*currently working* exit nodes it can rotate through when a free-tier run hits a
per-IP limit, with each candidate proven alive before it is written.

Usage:
    python scripts/proxy_harvest.py                # default limits
    python scripts/proxy_harvest.py --limit 200 --workers 40
    python scripts/proxy_harvest.py --dry-run      # fetch lists, check nothing

Output format is one URL per line, which is what ProxyPool.load() expects.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
import time
from pathlib import Path

import httpx

SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
]
JUDGES = ("https://api.ipify.org?format=json", "https://example.com/")
LINE = re.compile(r"^(?:[a-z0-9]+://)?(?:\S+@)?[\w.\-]+:\d{2,5}$", re.IGNORECASE)


async def fetch_list(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        response = await client.get(url, timeout=20)
    except httpx.HTTPError as exc:
        print(f"  ! {url.split('/')[-2]}/{url.split('/')[-1]}: {exc}", file=sys.stderr)
        return []
    if response.status_code != 200:
        print(f"  ! {url}: HTTP {response.status_code}", file=sys.stderr)
        return []
    out = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            line = "http://" + line
        if LINE.match(line.split("://", 1)[1]):
            out.append(line)
    print(f"  + {url.split('/')[-1]}: {len(out)} candidates")
    return out


async def check(proxy: str, timeout_seconds: float) -> tuple[str, float] | None:
    started = time.monotonic()
    for judge in JUDGES:
        try:
            async with httpx.AsyncClient(
                proxy=proxy, timeout=timeout_seconds, follow_redirects=True
            ) as client:
                response = await client.get(judge)
        except Exception:  # noqa: BLE001 - try the next judge
            continue
        if response.status_code == 200 and len(response.text) > 20:
            return proxy, time.monotonic() - started
    return None


def _write_pool(path: Path, proxies: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(proxies) + ("\n" if proxies else ""), encoding="utf-8")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/proxies.txt")
    parser.add_argument("--limit", type=int, default=300, help="candidates to test")
    parser.add_argument("--workers", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--dry-run", action="store_true", help="list only, no network check")
    args = parser.parse_args()

    print("fetching public lists ...")
    async with httpx.AsyncClient(headers={"User-Agent": "leadforge-proxy-harvest/1.0"}) as client:
        lists = await asyncio.gather(*(fetch_list(client, url) for url in SOURCES))

    candidates = sorted({entry for chunk in lists for entry in chunk})
    random.shuffle(candidates)
    candidates = candidates[: args.limit]
    print(f"unique candidates: {len(candidates)} (testing {len(candidates)})")

    if args.dry_run:
        for entry in candidates[:10]:
            print("  ", entry)
        return 0

    semaphore = asyncio.Semaphore(args.workers)
    alive: list[tuple[str, float]] = []

    async def worker(proxy: str) -> None:
        async with semaphore:
            result = await check(proxy, args.timeout)
            if result:
                alive.append(result)
                print(f"  ok {result[0]} ({result[1]:.1f}s)")

    await asyncio.gather(*(worker(proxy) for proxy in candidates))
    alive.sort(key=lambda item: item[1])

    out = Path(args.out)
    await asyncio.to_thread(_write_pool, out, [proxy for proxy, _ in alive])
    print(f"\nwrote {len(alive)} working proxies to {out}")
    if not alive:
        print(
            "none survived - free lists are like that. Try again later or use WARP/Tor.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
