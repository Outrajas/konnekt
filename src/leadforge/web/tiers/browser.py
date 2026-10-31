"""Tier 3: a real, optionally headed browser driven through Patchright/Playwright.

Why this tier exists
--------------------
Tier 1 (impersonating HTTP) reads everything that ships HTML. Tier 3 reads what
only exists after JavaScript runs, what sits behind a TLS/CDP fingerprint check,
or what needs a signed-in session. It is the "be a real browser" tier.

Guardrails that stay on
-----------------------
* Opt-in twice: ``LEADFORGE_BROWSER_ENABLED=true`` in the environment *and*
  ``allow_browser = true`` on the domain rule.
* The persistent profile directory is the operator's own browser profile. Code
  never types, stores or reads credentials; the human signs in once, by hand,
  in the visible window.
* A challenge page in a headed session is handed back to the human (bounded
  wait) instead of being solved programmatically.
* Every navigation still goes through the router's budget, cache, policy and
  run-log bookkeeping; this module only replaces the transport.
"""

from __future__ import annotations

import logging

from leadforge.web.errors import FetchFailed, TierUnavailable
from leadforge.web.tiers.http import MAX_RESPONSE_BYTES, RawResponse

log = logging.getLogger(__name__)

CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies to continue",
    "cf-challenge",
    "captcha-delivery",
    "px-captcha",
    "unusual traffic",
    "access denied",
)

_SESSION: BrowserSession | None = None


def _load_engine(preferred: str = "auto"):
    """Return the Playwright-compatible async API, preferring Patchright."""
    order = ("patchright", "playwright") if preferred == "auto" else (preferred,)
    errors = []
    for name in order:
        try:
            module = __import__(f"{name}.async_api", fromlist=["async_api"])
        except ImportError as exc:  # pragma: no cover - depends on install
            errors.append(f"{name}: {exc}")
            continue
        return module
    raise TierUnavailable("No browser engine installed (" + "; ".join(errors) + ")")


def engine_status(preferred: str = "auto") -> dict[str, object]:
    """Report which engine would be used, without launching anything."""
    try:
        module = _load_engine(preferred)
    except TierUnavailable as exc:
        return {"available": False, "engine": None, "detail": str(exc)}
    return {"available": True, "engine": module.__name__.split(".")[0], "detail": "ok"}


def looks_like_challenge(html: str) -> bool:
    lowered = (html or "")[:20000].lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def stealth_args() -> list[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--lang=en-IN",
    ]


class BrowserSession:
    """One persistent browser context per process, reused across navigations."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._engine = None
        self._context = None
        self._pages = 0

    async def start(self):
        if self._context is not None:
            return self._context
        self._engine = _load_engine(self.settings.browser_engine)
        profile = self.settings.browser_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        options = {
            "user_data_dir": str(profile),
            "headless": not self.settings.browser_headed,
            "locale": self.settings.browser_locale,
            "timezone_id": self.settings.browser_timezone,
            "viewport": {
                "width": self.settings.browser_viewport_width,
                "height": self.settings.browser_viewport_height,
            },
            "args": stealth_args(),
            "accept_downloads": False,
            "ignore_https_errors": False,
        }
        if self.settings.browser_channel:
            options["channel"] = self.settings.browser_channel
        if self.settings.browser_user_agent:
            options["user_agent"] = self.settings.browser_user_agent
        try:
            self._context = await self._engine.chromium.launch_persistent_context(**options)
        except Exception as exc:  # noqa: BLE001 - surfaced as a tier failure
            raise TierUnavailable(f"Browser launch failed: {exc}") from exc
        if callable(getattr(self._context, "set_default_timeout", None)):
            self._context.set_default_timeout(int(self.settings.browser_timeout_seconds * 1000))
        log.info("tier3 browser started engine=%s headed=%s",
                 self._engine.__name__, self.settings.browser_headed)
        return self._context

    async def navigate(self, url: str) -> RawResponse:
        context = await self.start()
        page = await context.new_page()
        try:
            response = await page.goto(
                url,
                wait_until=self.settings.browser_wait_until,
                timeout=int(self.settings.browser_timeout_seconds * 1000),
            )
            if self.settings.browser_settle_ms:
                await page.wait_for_timeout(self.settings.browser_settle_ms)
            body = await page.content()
            if looks_like_challenge(body) and self.settings.browser_headed:
                body = await self._wait_for_human(page, body)
                if looks_like_challenge(body):
                    raise FetchFailed("Challenge was not cleared inside the wait window")
            status = getattr(response, "status", 200) if response is not None else 200
            final_url = getattr(page, "url", url) or url
        except TierUnavailable:
            raise
        except FetchFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors are fetch errors
            raise FetchFailed(f"Browser navigation failed for {url}: {exc}") from exc
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001 - closing must never mask the result
                pass
        self._pages += 1
        if len(body.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
            raise FetchFailed("Browser response exceeded 3 MB limit")
        return RawResponse(final_url, int(status), {}, body, "browser")

    async def _wait_for_human(self, page, body: str) -> str:
        """Bounded wait for the operator to clear a challenge in the visible window."""
        budget_ms = int(self.settings.browser_manual_challenge_wait_s * 1000)
        if budget_ms <= 0:
            return body
        log.warning(
            "tier3: challenge detected; solve it in the browser window "
            "(waiting up to %ss)",
            self.settings.browser_manual_challenge_wait_s,
        )
        waited = 0
        step = 2000
        while waited < budget_ms and looks_like_challenge(body):
            await page.wait_for_timeout(step)
            waited += step
            try:
                body = await page.content()
            except Exception:  # noqa: BLE001 - page may be navigating
                break
        if looks_like_challenge(body):
            raise FetchFailed("Challenge was not cleared inside the wait window")
        return body

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            finally:
                self._context = None


async def get_session(settings) -> BrowserSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = BrowserSession(settings)
    return _SESSION


async def close_session() -> None:
    global _SESSION
    if _SESSION is not None:
        await _SESSION.close()
        _SESSION = None


async def fetch(url: str, *, settings, rule) -> RawResponse:
    """Tier-3 entry point used by the fetch router."""
    if not getattr(settings, "browser_enabled", False):
        raise TierUnavailable("Tier 3 is disabled; set LEADFORGE_BROWSER_ENABLED=true")
    if not getattr(rule, "allow_browser", False):
        raise TierUnavailable("Tier 3 is disabled for this domain by policy")
    if getattr(rule, "max_tier", 0) < 3:
        raise TierUnavailable("Tier 3 exceeds this domain's max_tier")
    session = await get_session(settings)
    if session._pages >= getattr(settings, "browser_max_pages_per_session", 40):
        await close_session()
        session = await get_session(settings)
    return await session.navigate(url)
