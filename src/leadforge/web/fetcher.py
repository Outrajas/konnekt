import asyncio
import logging
from dataclasses import replace
from urllib.parse import urlsplit

import httpx

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect
from leadforge.web import FetchResult
from leadforge.web.budget import Budget
from leadforge.web.cache import PageCache, normalize_url
from leadforge.web.denylist import check_host, load_denylist
from leadforge.web.detect import classify_response
from leadforge.web.errors import (
    Blocked,
    DomainPaused,
    FetchFailed,
    TierUnavailable,
)
from leadforge.web.policy import DomainPolicy, DomainRule
from leadforge.web.robots import RobotsChecker
from leadforge.web.tiers import http, obscura, stealth

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.policy = DomainPolicy.load(self.settings.domain_policy_path, self.settings)
        self.denylist = load_denylist(self.settings.denylist_hosts_path)
        self.budget = Budget(str(self.settings.db_path))
        self.cache = PageCache(self.settings)
        self.robots = RobotsChecker(self.settings.user_agent, self.settings.http_timeout_seconds)
        self.robots.request_fn = lambda robots_url: http.fetch(robots_url, self.settings)
        self.robots.before_request = lambda robots_host: self.budget.acquire(
            self.policy.budget_key(robots_host), self.policy.resolve(robots_host)
        )
        self.robots.cache_get = self.cache.get
        self.robots.cache_put = self.cache.put

    async def fetch(
        self,
        url: str,
        *,
        min_tier: int = 1,
        max_tier: int | None = None,
        use_cache: bool = True,
        purpose: str = "",
    ) -> FetchResult:
        normalized = normalize_url(url)
        parts = urlsplit(normalized)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Only absolute http(s) URLs are supported")
        host = parts.hostname.lower()
        check_host(host, self.denylist)
        rule = self.policy.resolve(host)
        if not rule.enabled:
            raise DomainPaused(f"{host} disabled by policy")
        budget_host = self.policy.budget_key(host)
        if budget_host == "linkedin.com" and purpose != "search-snippet-miss":
            raise DomainPaused("LinkedIn page fetch requires purpose='search-snippet-miss'")
        if budget_host in {"indeed.com", "glassdoor.com"} and purpose not in {
            "job",
            "hiring-signal",
        }:
            raise DomainPaused(f"{budget_host} fetches are limited to job and hiring signals")
        cached = self.cache.get(normalized) if use_cache else None
        if cached is not None:
            self._log_fetch(normalized, cached.tier, cached.status, "ok", True, purpose)
            return cached
        if rule.obey_robots and not await self.robots.allowed(normalized):
            raise DomainPaused(f"robots.txt disallows fetching {host}")
        if host in self.robots.crawl_delays:
            delay = self.robots.crawl_delays[host]
            rule = replace(rule, min_delay_s=max(rule.min_delay_s, delay))
        tier = max(1, min_tier)
        requested_ceiling = rule.max_tier if max_tier is None else max_tier
        ceiling = min(requested_ceiling, rule.max_tier)
        if tier > ceiling:
            raise TierUnavailable(
                f"Requested tier {tier} exceeds the permitted maximum tier {ceiling} for {host}"
            )
        while True:
            await self.budget.acquire(budget_host, rule)
            try:
                response = await self._fetch_tier(tier, normalized, rule)
            except (TimeoutError, OSError, httpx.HTTPError) as exc:
                # One bounded retry; each attempt acquires budget independently.
                if getattr(exc, "_leadforge_retried", False):
                    raise FetchFailed(f"Fetch failed for {host}: {exc}") from exc
                try:
                    await asyncio.sleep(0.25)
                    await self.budget.acquire(budget_host, rule)
                    response = await self._fetch_tier(tier, normalized, rule)
                except Exception as retry_exc:
                    raise FetchFailed(f"Fetch failed for {host}: {retry_exc}") from retry_exc
            classification = classify_response(response.status, response.headers, response.body)
            self._log_fetch(normalized, tier, response.status, classification, False, purpose)
            if classification == "ok":
                result = FetchResult(
                    normalized,
                    response.url,
                    response.status,
                    response.body,
                    "",
                    tier,
                    False,
                    False,
                    clock.utcnow_iso(),
                )
                self.cache.put(result)
                return result
            if classification == "thin" and tier < ceiling:
                tier += 1
                continue
            if classification in ("blocked", "challenge", "rate_limited"):
                await self.budget.pause(
                    self.policy.budget_key(host), self.settings.pause_hours_on_block, classification
                )
                raise Blocked(
                    f"{host} returned {classification}; paused for "
                    f"{self.settings.pause_hours_on_block} hours"
                )
            if classification == "thin":
                raise FetchFailed(f"Thin response from {host}; no permitted rendering tier")
            raise FetchFailed(f"Unexpected response from {host}: {response.status}")

    async def _fetch_tier(self, tier: int, url: str, rule: DomainRule) -> http.RawResponse:
        if tier == 1:
            host = urlsplit(url).hostname or ""
            api_hosts = {
                "boards-api.greenhouse.io",
                "api.lever.co",
                "api.ashbyhq.com",
                "hacker-news.firebaseio.com",
                "hn.algolia.com",
            }
            if host in api_hosts:
                return await http.fetch(url, self.settings, use_httpx=True)
            return await http.fetch(url, self.settings)
        if tier == 2:
            return await obscura.fetch(
                url,
                executable=str(self.settings.obscura_path) if self.settings.obscura_path else None,
                stealth=self.settings.obscura_stealth,
                timeout_seconds=self.settings.obscura_timeout_seconds,
            )
        if tier == 3:
            stealth.require_allowed(rule)
        raise TierUnavailable(f"Unsupported fetch tier: {tier}")

    def _log_fetch(
        self,
        url: str,
        tier: int,
        status: int | None,
        classification: str,
        from_cache: bool,
        purpose: str,
    ) -> None:
        parts = urlsplit(url)
        safe_url = f"{parts.scheme}://{parts.netloc}{parts.path}"
        conn = connect(self.settings.db_path)
        try:
            conn.execute(
                "INSERT INTO run_log(ts,stage,level,message) VALUES(?,?,?,?)",
                (
                    clock.utcnow_iso(),
                    "fetch",
                    "info",
                    f"host={parts.hostname} tier={tier} status={status} class={classification} "
                    f"cache={from_cache} purpose={purpose[:80]} url={safe_url}",
                ),
            )
            conn.commit()
        finally:
            conn.close()


async def fetch(url: str, **kwargs) -> FetchResult:
    return await Fetcher().fetch(url, **kwargs)


async def fetch_text(url: str, **kwargs) -> str:
    result = await fetch(url, **kwargs)
    from bs4 import BeautifulSoup

    return " ".join(BeautifulSoup(result.html, "lxml").get_text(" ").split())
