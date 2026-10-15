"""Bounded, auditable person-page walk with injectable I/O boundaries."""

from __future__ import annotations

import inspect
import re
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from leadforge.llm.client import LLMNotConfigured
from leadforge.llm.schemas import PageExtraction
from leadforge.llm.validate import validate_page_extraction
from leadforge.models import Contact, EmailCandidate, VerificationMethod, VerificationVerdict
from leadforge.scoring import score
from leadforge.tools.email_patterns import generate
from leadforge.web.denylist import check_host
from leadforge.web.errors import Blocked, DeniedHost, DomainPaused, FetchFailed
from leadforge.web.extract import (
    FoundEmail,
    Link,
    classify_host,
    clean_text,
    extract_emails,
    extract_links,
    registrable_domain,
)

WalkStatus = Literal["found", "not_found"]
WalkStopReason = Literal[
    "email_found", "budget", "stale", "denied", "no_edges", "time", "max_depth"
]
FetchFn = Callable[[str], Awaitable[Any] | Any]
LLMFn = Callable[..., Awaitable[PageExtraction] | PageExtraction]
VerifyFn = Callable[[list[EmailCandidate]], Awaitable[list[EmailCandidate]] | list[EmailCandidate]]


@dataclass(frozen=True)
class WalkBudget:
    max_fetches: int = 8
    max_depth: int = 3
    stale_hops: int = 2
    wall_clock_s: int = 600


@dataclass
class WalkResult:
    person_id: int
    status: WalkStatus
    stop_reason: str
    candidates: list[EmailCandidate]
    pages_visited: list[str]
    evidence: list[tuple[str, str, bool]] = field(default_factory=list)


@dataclass(frozen=True)
class WalkPage:
    url: str
    html: str
    status: int = 200


_ALLOWED_EDGE_KINDS = {
    "personal_site", "github", "gitlab", "talk", "mail_archive", "company"
}
_TEAM_PATHS = (
    "/team", "/about", "/about-us", "/company", "/people", "/leadership",
    "/contact", "/careers",
)


def seed_urls(company_domain: str, profile_links: list[str] | None = None) -> list[str]:
    """Build stable company-page seeds, then append previously supplied profiles."""
    domain = company_domain.strip().strip("/")
    scheme_host = domain if "://" in domain else f"https://{domain}"
    generated = [scheme_host + path for path in _TEAM_PATHS]
    return list(dict.fromkeys([*generated, *(profile_links or [])]))


async def walk_person(
    *,
    person_id: int,
    contact: Contact,
    fetch_fn: FetchFn,
    llm_fn: LLMFn,
    verify_fn: VerifyFn,
    clock: Callable[[], float] = time.monotonic,
    budget: WalkBudget | None = None,
    seed_urls_override: list[str] | None = None,
    denylist_hosts: set[str] | None = None,
    persist_fn: Callable[[WalkResult], Any] | None = None,
    min_confidence_to_emit: float = 0.4,
    established_pattern: str | None = None,
) -> WalkResult:
    """Run a bounded walk. All network/model/clock/persistence dependencies are injected."""
    limits = budget or WalkBudget()
    denied = {host.lower().rstrip(".") for host in (denylist_hosts or set())}
    queue: deque[tuple[str, int, bool]] = deque(
        (url, 0, False)
        for url in (seed_urls_override or seed_urls(contact.domain))
    )
    visited: set[str] = set()
    pages: list[str] = []
    candidates: dict[str, EmailCandidate] = {}
    evidence: list[tuple[str, str, bool]] = []
    stale_count = 0
    started = clock()
    stop_reason: WalkStopReason = "no_edges"

    # The established domain pattern is the free pre-walk candidate; it is verified first.
    if established_pattern:
        pattern_candidates = generate(contact, [established_pattern])
        if pattern_candidates:
            checked = await _maybe_await(verify_fn(pattern_candidates))
            if checked:
                checked_candidate = checked[0]
                if (
                    checked_candidate.verification_method is VerificationMethod.SMTP
                    and checked_candidate.verification_verdict is VerificationVerdict.SEND
                    and checked_candidate.catch_all_domain is not True
                ):
                    checked_candidate.evidence_tier = "B"
                    checked_candidate.confidence = score(
                        checked_candidate, source_count=1, pattern_matches_domain=True
                    )
                    if checked_candidate.confidence >= min_confidence_to_emit:
                        candidates[checked_candidate.email] = checked_candidate
                        result = WalkResult(
                            person_id=person_id,
                            status="found",
                            stop_reason="email_found",
                            candidates=[checked_candidate],
                            pages_visited=[],
                            evidence=[("pattern_sample", established_pattern, True)],
                        )
                        if persist_fn is not None:
                            await _maybe_await(persist_fn(result))
                        return result

    while queue:
        if len(pages) >= limits.max_fetches:
            stop_reason = "budget"
            break
        if clock() - started >= limits.wall_clock_s:
            stop_reason = "time"
            break
        url, depth, reached_from_accepted = queue.popleft()
        if url in visited:
            continue
        if depth >= limits.max_depth:
            stop_reason = "max_depth"
            continue
        host = (urlsplit(url).hostname or "").lower()
        try:
            check_host(host, denied)
        except DeniedHost:
            stop_reason = "denied"
            continue
        if not host:
            continue
        visited.add(url)
        before_evidence = len(evidence)
        try:
            raw = await _maybe_await(fetch_fn(url))
        except (DeniedHost, DomainPaused, Blocked):
            stop_reason = "denied"
            break
        except (FetchFailed, PermissionError):
            stale_count += 1
            if stale_count >= limits.stale_hops:
                stop_reason = "stale"
                break
            continue
        page = _as_page(url, raw)
        if page.status >= 400:
            stale_count += 1
            if stale_count >= limits.stale_hops:
                stop_reason = "stale"
                break
            continue
        pages.append(url)
        text = clean_text(page.html, url=url)
        links = extract_links(page.html, url, denylist=denied)
        found_emails = extract_emails(page.html + "\n" + text)
        host_kind = classify_host(url, company_domain=contact.domain)
        target_named = _contains_name(text, contact)
        company_team_page = (
            host_kind == "company"
            and registrable_domain(host) == registrable_domain(contact.domain)
            and target_named
        )
        person_controlled = (
            host_kind in {"personal_site", "github", "gitlab"}
            and target_named
            and reached_from_accepted
        )
        accepted_page = company_team_page or person_controlled

        extraction: PageExtraction | None = None
        if accepted_page:
            try:
                proposed = await _maybe_await(
                    llm_fn(
                        contact=contact,
                        page_text=text,
                        candidate_links=links,
                        url=url,
                    )
                )
                if isinstance(proposed, PageExtraction):
                    extraction = validate_page_extraction(
                        proposed,
                        cleaned_text=text,
                        extracted_emails=found_emails,
                        candidate_links=links,
                        denylist_hosts=denied,
                        page_host=host,
                    )
            except LLMNotConfigured:
                extraction = None

        published = _target_emails(contact, text, found_emails, extraction)
        if accepted_page and published:
            for found in published:
                candidate = EmailCandidate(
                    email=found.email,
                    contact=contact,
                    source="public_web",
                    found_on_web=True,
                    evidence_tier="A",
                    verification_method=VerificationMethod.UNVERIFIED,
                    verification_verdict=VerificationVerdict.UNKNOWN,
                    confidence=0.25,
                    notes=f"Published beside target name on {url}",
                )
                candidates[found.email] = candidate
                evidence.append(("email_published", found.email, True))
                stop_reason = "email_found"
            if stop_reason == "email_found":
                break

        if accepted_page:
            proposed_order = [item.url for item in extraction.next_links] if extraction else []
            allowed_links: list[Link] = []
            for link in links:
                link_host = urlsplit(link.url).hostname or ""
                try:
                    check_host(link_host, denied)
                except DeniedHost:
                    continue
                kind = classify_host(link.url, company_domain=contact.domain)
                if kind in _ALLOWED_EDGE_KINDS:
                    allowed_links.append(link)
            allowed_links.sort(
                key=lambda link: (
                    (
                        proposed_order.index(link.url)
                        if link.url in proposed_order
                        else len(proposed_order) + 1
                    ),
                    _heuristic_rank(link.anchor_text, contact),
                    link.url,
                )
            )
            for link in allowed_links:
                if link.url not in visited and all(link.url != queued[0] for queued in queue):
                    queue.append((link.url, depth + 1, True))
                    evidence.append(("profile_link", link.url, True))

        if len(evidence) == before_evidence:
            stale_count += 1
        else:
            stale_count = 0
        if stale_count >= limits.stale_hops:
            stop_reason = "stale"
            break

    if stop_reason not in {"email_found", "budget", "stale", "time", "denied", "max_depth"}:
        stop_reason = "no_edges" if not queue else "budget"
    result_candidates = list(candidates.values())
    if not result_candidates:
        result_candidates = generate(contact)
        if verify_fn:
            verified = await _maybe_await(verify_fn(result_candidates))
            result_candidates = list(verified)
        for candidate in result_candidates:
            candidate.confidence = score(
                candidate, source_count=1, pattern_matches_domain=False
            )
    result = WalkResult(
        person_id=person_id,
        status=(
            "found"
            if any(
                candidate.evidence_tier == "A"
                or (
                    candidate.evidence_tier == "B"
                    and candidate.confidence >= min_confidence_to_emit
                    and candidate.verification_verdict is not VerificationVerdict.KILL
                    and candidate.catch_all_domain is not True
                )
                for candidate in result_candidates
            )
            else "not_found"
        ),
        stop_reason=stop_reason,
        candidates=result_candidates,
        pages_visited=pages,
        evidence=evidence,
    )
    if persist_fn is not None:
        await _maybe_await(persist_fn(result))
    return result


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _as_page(requested_url: str, raw: Any) -> WalkPage:
    if isinstance(raw, WalkPage):
        return raw
    if isinstance(raw, str):
        return WalkPage(requested_url, raw)
    return WalkPage(
        url=getattr(raw, "url", requested_url),
        html=getattr(raw, "html", getattr(raw, "body", "")),
        status=int(getattr(raw, "status", getattr(raw, "status_code", 200)) or 200),
    )


def _tokens(value: str) -> set[str]:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return set(re.findall(r"[a-z0-9]+", plain))


def _contains_name(text: str, contact: Contact) -> bool:
    target = _tokens(contact.full_name)
    return bool(target) and target.issubset(_tokens(text))


def _target_emails(
    contact: Contact,
    text: str,
    found: list[FoundEmail],
    extraction: PageExtraction | None,
) -> list[FoundEmail]:
    target_tokens = _tokens(contact.full_name)
    if extraction is not None:
        for person in extraction.people:
            if target_tokens and target_tokens == _tokens(person.name):
                named = {email.lower() for email in person.emails_published}
                matching = [item for item in found if item.email.lower() in named]
                if matching:
                    return matching
    paragraphs = [paragraph for paragraph in re.split(r"\n+", text) if paragraph]
    for paragraph in paragraphs:
        if not target_tokens.issubset(_tokens(paragraph)):
            continue
        nearby = [item for item in found if item.email in paragraph.lower()]
        if len(nearby) == 1:
            return nearby
    return []


def _heuristic_rank(anchor: str, contact: Contact) -> int:
    value = anchor.casefold()
    hints = (
        "contact", "about", "blog", "talk", "github", *contact.full_name.casefold().split()
    )
    return min(
        (index for index, hint in enumerate(hints) if hint and hint in value),
        default=len(hints),
    )
