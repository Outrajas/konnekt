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

Engine selection
----------------
Patchright is preferred because it removes the ``Runtime.enable``/CDP artefacts
anti-bot vendors fingerprint. A Patchright install that has no browser build
downloaded (``patchright install chromium`` never run) is useless on its own, so
startup walks a ladder: patchright → playwright, and configured ``channel`` →
bundled Chromium. The first combination that launches wins, and the reason the
others failed is kept for the error message.
"""

from __future__ import annotations

import importlib
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


def _engine_order(preferred: str = "auto") -> tuple[str, ...]:
    return (preferred,) if preferred != "auto" else ("patchright", "playwright")


def _load_engine(name: str):
    """Import one engine's async API module, or raise ImportError."""
    module = importlib.import_module(f"{name}.async_api")
    if not hasattr(module, "async_playwright"):
        raise ImportError(f"{name}.async_api has no async_playwright")
    return module


def engine_status(preferred: str = "auto") -> dict[str, object]:
    """Report which engines are importable, without launching anything."""
    importable, errors = [], []
    for name in _engine_order(preferred):
        try:
            _load_engine(name)
        except ImportError as exc:  # pragma: no cover - depends on install
            errors.append(f"{name}: {exc}")
            continue
        importable.append(name)
    if not importable:
        return {"available": False, "engine": None, "detail": "; ".join(errors)}
    return {"available": True, "engine": importable[0], "detail": ",".join(importable)}


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
        self._playwright = None
        self._context = None
        self._pages = 0
        self._launch_notes: list[str] = []

    def _context_options(self, channel: str | None) -> dict:
        profile = self.settings.browser_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        options: dict = {
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
        if channel:
            options["channel"] = channel
        if self.settings.browser_user_agent:
            options["user_agent"] = self.settings.browser_user_agent
        return options

    def _channel_candidates(self) -> tuple[str | None, ...]:
        configured = self.settings.browser_channel or None
        return (configured, None) if configured else (None,)

    async def start(self):
        if self._context is not None:
            return self._context
        for engine_name in _engine_order(self.settings.browser_engine):
            try:
                module = _load_engine(engine_name)
            except ImportError as exc:
                self._launch_notes.append(f"{engine_name}: {exc}")
                continue
            for channel in self._channel_candidates():
                try:
                    playwright = await module.async_playwright().start()
                except Exception as exc:  # noqa: BLE001
                    self._launch_notes.append(f"{engine_name}: start failed: {exc}")
                    break
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        **self._context_options(channel)
                    )
                except Exception as exc:  # noqa: BLE001 - try the next rung
                    self._launch_notes.append(
                        f"{engine_name}/channel={channel}: {str(exc).splitlines()[0][:180]}"
                    )
                    try:
                        await playwright.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                self._engine, self._playwright, self._context = module, playwright, context
                if callable(getattr(context, "set_default_timeout", None)):
                    context.set_default_timeout(
                        int(self.settings.browser_timeout_seconds * 1000)
                    )
                log.info(
                    "tier3 browser started engine=%s channel=%s headed=%s",
                    engine_name,
                    channel,
                    self.settings.browser_headed,
                )
                return context
        detail = " | ".join(self._launch_notes[-4:]) or "no engine available"
        raise TierUnavailable(f"Browser launch failed: {detail}")

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
        except (TierUnavailable, FetchFailed):
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
        return body

    async def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
            self._playwright = None

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            finally:
                self._context = None
        await self._stop_playwright()


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
