"""Agentic people finder.

The LLM drives traversal: given the current page summary and its candidate
links, it picks the next URL to visit (or stops). Deterministic people
extraction runs in parallel on every page. The LLM only proposes people on
pages where the deterministic extractor returns nothing.

Hard budgets: max_pages and max_tokens. Whichever triggers first, return
what we have. Attribution is checked on every LLM-proposed person.

Uses leadforge.llm.chat_json(system, user, schema=AgentDecision) which is
async and returns an AgentDecision instance (or raises LLMNotConfigured /
LLMOutputError).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

from leadforge.models import PersonCandidate
from leadforge.sources.people import _is_probable_name, extract_people

log = logging.getLogger(__name__)


# ---------------- Pydantic schema for the LLM call ----------------


class DecisionPerson(BaseModel):
    name: str = ""
    title: str = ""
    html_anchor: str = ""


class AgentDecision(BaseModel):
    people: list[DecisionPerson] = Field(default_factory=list)
    next_url: str | None = None
    done: bool = False
    reasoning: str = ""


# ---------------- link ranking ----------------


_CANDIDATE_LINK_HINTS: tuple[str, ...] = (
    "team", "people", "about", "staff", "leadership", "company",
    "contact", "founders", "management", "who-we-are",
)

_LINK_SKIP_HINTS: tuple[str, ...] = (
    "login", "signup", "signin", "register", "privacy", "terms",
    "cookies", "policy", "pricing", "blog", "news", "press",
    "careers", "jobs", "product", "products", "features", "docs",
    "documentation", "support", "help", "faq", "sitemap",
)

_SKIP_HOSTS: frozenset[str] = frozenset({
    "twitter.com", "x.com", "facebook.com", "linkedin.com",
    "instagram.com", "youtube.com", "tiktok.com",
    "medium.com", "substack.com",
})


# ---------------- dataclasses ----------------


@dataclass
class AgentPage:
    url: str
    people_found: int
    llm_decision: str
    tokens_used: int


@dataclass
class AgentRunResult:
    company: str
    people: list[PersonCandidate] = field(default_factory=list)
    pages: list[AgentPage] = field(default_factory=list)
    tokens_used: int = 0
    pages_fetched: int = 0
    stop_reason: str = ""


# ---------------- helpers ----------------


def _summarize_page(html: str, base_url: str, *, max_chars: int = 3000) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    chunks: list[str] = []
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        if not isinstance(h, Tag):
            continue
        heading = " ".join(h.get_text(" ", strip=True).split())
        if heading:
            chunks.append(f"## {heading}")
        sib = h.find_next_sibling()
        hops = 0
        while sib is not None and hops < 2:
            if isinstance(sib, Tag):
                text = " ".join(sib.get_text(" ", strip=True).split())
                if text and len(text) < 300:
                    chunks.append(text)
            sib = sib.find_next_sibling()
            hops += 1

    if not chunks:
        text = " ".join(soup.get_text(" ", strip=True).split())
        chunks.append(text[:max_chars])

    return "\n".join(chunks)[:max_chars]


def _candidate_links(html: str, base_url: str, *, limit: int = 15) -> list[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    base_host = (urlparse(base_url).hostname or "").lower()
    seen: set[str] = set()
    scored: list[tuple[int, str]] = []

    for a in soup.find_all("a", href=True):
        if not isinstance(a, Tag):
            continue
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        low = href.lower()
        if low.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.hostname and parsed.hostname.lower() != base_host:
            continue
        if any(skip in full.lower() for skip in _SKIP_HOSTS):
            continue
        if full in seen:
            continue
        seen.add(full)

        path_low = (parsed.path or "").lower()
        if any(skip in path_low for skip in _LINK_SKIP_HINTS):
            continue

        score = 0
        for hint in _CANDIDATE_LINK_HINTS:
            if hint in path_low:
                score += 2
                break
        if len(path_low) < 40:
            score += 1
        scored.append((score, full))

    scored.sort(reverse=True)
    return [url for _, url in scored[:limit]]


def _estimate_tokens(obj) -> int:
    """Approximate tokens for a Pydantic response. The real client doesn't
    return usage counts from chat_json, so we approximate from string length."""
    try:
        text = obj.model_dump_json() if hasattr(obj, "model_dump_json") else str(obj)
    except Exception:  # noqa: BLE001
        text = str(obj)
    return max(1, len(text) // 4)


# Explicit nav/product word blocklist used in addition to _is_probable_name.
# Belt-and-suspenders: even if _is_probable_name lets something through, this
# catches obvious nav/product phrases the LLM might return.
_NAV_BLOCKLIST: frozenset[str] = frozenset({
    "compare", "feature", "features", "plan", "plans", "pricing",
    "product", "products", "solution", "solutions", "service", "services",
    "resource", "resources", "docs", "documentation", "api", "sdk",
    "login", "signup", "signin", "register", "account",
    "blog", "news", "press", "case", "study", "stories", "customer",
    "customers", "startup", "startups", "enterprise", "business",
    "claude", "chatgpt", "gpt", "gemini", "copilot", "code", "science",
    "slides", "artifacts", "model", "models", "agent", "agents",
    "information", "details", "read", "view", "see", "learn",
    "create", "build", "ship", "start", "get", "try", "buy", "sell",
    "and", "or", "but", "for", "with", "from", "about", "into",
    "contact", "team", "careers", "jobs", "home", "menu", "search",
})


def _attribution_ok(proposed: DecisionPerson, html: str) -> bool:
    if not proposed.name:
        return False
    # Direct stoplist: reject if ANY word is a nav/product term.
    # "Compare Features Across Plans" -> rejected on "compare".
    name_words = {w.lower() for w in proposed.name.split() if w}
    if name_words & _NAV_BLOCKLIST:
        return False
    if not _is_probable_name(proposed.name):
        return False
    if proposed.name.lower() not in html.lower():
        return False
    if proposed.title and proposed.title.lower() not in html.lower():
        return False
    return True


def _json_from_text(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
        raise ValueError(f"no JSON object in response: {text[:200]}")
    return json.loads(text[brace_start : brace_end + 1])


# ---------------- LLM call adapter ----------------


async def _call_decision(
    system: str, user: str, *, llm_client=None
) -> tuple[AgentDecision, int]:
    """Call chat_json with the real signature; handle fakes and the real module."""
    # Case 1: caller supplied an object (fake or custom)
    if llm_client is not None:
        chat_json_fn = getattr(llm_client, "chat_json", None)
        if chat_json_fn is None:
            raise RuntimeError("llm_client has no chat_json method")
        resp = chat_json_fn(system, user, schema=AgentDecision)
        if hasattr(resp, "__await__"):
            resp = await resp
        if isinstance(resp, AgentDecision):
            return resp, _estimate_tokens(resp)
        if isinstance(resp, dict):
            decision = AgentDecision(**resp)
            return decision, _estimate_tokens(decision)
        if isinstance(resp, str):
            decision = AgentDecision(**_json_from_text(resp))
            return decision, _estimate_tokens(decision)
        raise RuntimeError(f"unexpected response type: {type(resp).__name__}")

    # Case 2: no client — use the real module function
    from leadforge.llm import chat_json as real_chat_json

    resp = real_chat_json(system, user, schema=AgentDecision)
    if hasattr(resp, "__await__"):
        resp = await resp
    return resp, _estimate_tokens(resp)


def _build_decide_system(goal: str) -> str:
    return (
        "You guide a web crawler to find people (HR, talent acquisition, "
        "engineering managers, recruiters) at a specific company.\n"
        f"GOAL: {goal}\n"
        "Rules:\n"
        "- Only list people whose name LITERALLY APPEARS in the page content.\n"
        "- Only include a title if it also literally appears near the name.\n"
        "- Pick next_url from the candidate links provided, or null to stop.\n"
        "- Set done=true when you have >=3 people, or the page has no useful "
        "new links, or the page is not a team/about page.\n"
        "- Never invent names, titles, or URLs."
    )


def _build_decide_user(
    company: str,
    current_url: str,
    summary: str,
    candidates: list[str],
    visited: list[str],
    people_so_far: int,
) -> str:
    visited_block = "\n".join(f"- {u}" for u in visited[-8:]) or "(none yet)"
    links_block = "\n".join(f"- {u}" for u in candidates) or "(none)"
    return f"""COMPANY: {company}
CURRENT_URL: {current_url}
PEOPLE FOUND SO FAR: {people_so_far}

RECENTLY VISITED:
{visited_block}

CANDIDATE LINKS:
{links_block}

PAGE CONTENT:
{summary}
"""


# ---------------- the loop ----------------


async def find_people_agentic(
    company: str,
    start_url: str,
    *,
    goal: str = "find HR, talent acquisition, or engineering managers",
    llm_client=None,
    fetcher=None,
    max_pages: int = 6,
    max_tokens: int = 30_000,
    require_llm: bool = False,
) -> AgentRunResult:
    """Recursive LLM-guided people discovery under hard budgets."""
    from leadforge.config import settings
    from leadforge.llm import LLMNotConfigured, LLMOutputError
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    if require_llm and (llm_client is None or llm_client is False):
        raise ValueError("require_llm=True but llm_client is None or False")

    router = fetcher or Fetcher(settings)
    state = AgentRunResult(company=company)
    visited: set[str] = set()
    current_url = start_url
    consecutive_dry = 0
    system_prompt = _build_decide_system(goal)

    for _ in range(max_pages):
        if state.tokens_used >= max_tokens:
            state.stop_reason = f"token_budget ({state.tokens_used} >= {max_tokens})"
            break
        if not current_url or current_url in visited:
            state.stop_reason = "visited_or_empty"
            break

        visited.add(current_url)
        try:
            resp = await router.fetch(current_url, purpose="person")
        except FetchError:
            state.pages.append(AgentPage(current_url, 0, "fetch_failed", 0))
            state.stop_reason = f"fetch_failed {current_url}"
            break

        html = resp.html or ""
        state.pages_fetched += 1

        # Deterministic pass on every page
        added = 0
        for p in extract_people(html, current_url, company):
            if not any(x.name.lower() == p.name.lower() for x in state.people):
                state.people.append(p)
                added += 1
        consecutive_dry = 0 if added > 0 else consecutive_dry + 1

        # Decide whether to use LLM this iteration.
        #   - llm_client=False:      force greedy (test sentinel)
        #   - llm_client=<object>:   use it
        #   - require_llm=True:      force LLM (client required, checked above)
        #   - llm_client=None:       auto-detect from env
        if llm_client is False:
            use_llm = False
        elif llm_client is not None:
            use_llm = True
        else:
            try:
                from leadforge.config import settings as _cfg
                use_llm = bool(
                    getattr(_cfg, "llm_api_key", None)
                    and getattr(_cfg, "llm_base_url", None)
                    and getattr(_cfg, "llm_model", None)
                )
            except Exception:  # noqa: BLE001
                use_llm = False

        if not use_llm:
            candidates = _candidate_links(html, current_url)
            next_url = next((u for u in candidates if u not in visited), "")
            state.pages.append(AgentPage(current_url, added, "greedy", 0))
            if not next_url:
                state.stop_reason = "no_candidates (greedy)"
                break
            if consecutive_dry >= 3:
                state.stop_reason = "3_dry_pages (greedy)"
                break
            current_url = next_url
            continue

        # LLM-guided
        summary = _summarize_page(html, current_url)
        candidates = _candidate_links(html, current_url)
        user_prompt = _build_decide_user(
            company=company,
            current_url=current_url,
            summary=summary,
            candidates=candidates,
            visited=list(visited),
            people_so_far=len(state.people),
        )

        try:
            decision, tokens = await _call_decision(
                system_prompt, user_prompt, llm_client=llm_client
            )
        except LLMNotConfigured as exc:
            state.pages.append(AgentPage(current_url, added, "llm_not_configured", 0))
            state.stop_reason = f"llm_not_configured: {exc}"
            break
        except LLMOutputError as exc:
            state.pages.append(AgentPage(current_url, added, "llm_output_error", 0))
            state.stop_reason = f"llm_output_error: {exc}"
            break
        except Exception as exc:  # noqa: BLE001
            state.pages.append(AgentPage(current_url, added, f"llm_error: {exc}", 0))
            state.stop_reason = f"llm_error: {exc}"
            break

        state.tokens_used += tokens

        # Attribution-checked LLM people
        for proposed in decision.people:
            if not _attribution_ok(proposed, html):
                continue
            name = proposed.name.strip()
            if any(x.name.lower() == name.lower() for x in state.people):
                continue
            state.people.append(
                PersonCandidate(
                    name=name,
                    title=proposed.title.strip(),
                    company=company,
                    url=current_url,
                    snippet="llm-extracted",
                    source="team-page-llm",
                )
            )

        state.pages.append(
            AgentPage(current_url, added, decision.reasoning or "", tokens)
        )

        # Post-decision budget check: if we just exceeded max_tokens,
        # report that as the stop reason (takes priority over llm_done).
        if state.tokens_used >= max_tokens:
            state.stop_reason = (
                f"token_budget ({state.tokens_used} >= {max_tokens})"
            )
            break

        if decision.done or not decision.next_url:
            state.stop_reason = f"llm_done: {decision.reasoning or '(no reason)'}"
            break
        if decision.next_url in visited:
            state.stop_reason = f"llm_revisit {decision.next_url}"
            break
        current_url = decision.next_url

    else:
        state.stop_reason = f"max_pages ({max_pages})"

    return state