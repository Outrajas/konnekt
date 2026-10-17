"""Resumable, bounded enrichment orchestration with injectable network/LLM/SMTP edges."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.models import Contact, EmailCandidate, EnrichmentResult
from leadforge.scoring import is_sendable
from leadforge.stages.pattern_learn import is_established
from leadforge.stages.walk import WalkBudget, WalkResult, walk_person

log = logging.getLogger(__name__)
FetchFn = Callable[[str], Awaitable[Any] | Any]
LLMFn = Callable[..., Awaitable[Any] | Any]
VerifyFn = Callable[[list[EmailCandidate]], Awaitable[list[EmailCandidate]] | list[EmailCandidate]]


async def run_enrichment(
    contacts: list[Contact],
    *,
    settings: Settings,
    fetch_fn: FetchFn,
    llm_fn: LLMFn,
    verify_fn: VerifyFn,
    persist_fn: Callable[[WalkResult], Awaitable[Any] | Any] | None = None,
    force: bool = False,
    max_concurrency: int = 3,
) -> list[EnrichmentResult]:
    """Process each company sequentially and up to three companies concurrently."""
    completed = set() if force else _completed_contacts(settings)
    groups: dict[str, list[tuple[int, Contact]]] = defaultdict(list)
    for index, contact in enumerate(contacts):
        groups[contact.domain.lower()].append((index, contact))
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    results: dict[int, EnrichmentResult] = {}

    async def run_company(group: list[tuple[int, Contact]]) -> None:
        async with semaphore:
            for index, contact in group:
                key = _contact_key(contact)
                if key in completed:
                    log.info(
                        "Skipping completed contact %s at %s",
                        contact.full_name,
                        contact.domain,
                    )
                    continue
                try:
                    walked = await walk_person(
                        person_id=0,
                        contact=contact,
                        fetch_fn=fetch_fn,
                        llm_fn=llm_fn,
                        verify_fn=verify_fn,
                        budget=WalkBudget(
                            settings.walk_max_fetches,
                            settings.walk_max_depth,
                            settings.walk_stale_hops,
                            settings.walk_wall_clock_seconds,
                        ),
                        persist_fn=persist_fn,
                        min_confidence_to_emit=settings.min_confidence_to_emit,
                        seed_urls_override=getattr(fetch_fn, "seeds", None) or None,
                        established_pattern=_established_pattern(settings, contact.domain),
                    )
                    results[index] = _as_enrichment(walked, settings)
                except Exception as exc:
                    log.exception(
                        "Enrichment failed for %s at %s", contact.full_name, contact.domain
                    )
                    results[index] = EnrichmentResult(
                        contact=contact,
                        status="not_found",
                        stop_reason=f"error: {type(exc).__name__}",
                    )
                    if persist_fn is not None:
                        failed = WalkResult(
                            person_id=0,
                            status="not_found",
                            stop_reason=f"error: {type(exc).__name__}",
                            candidates=[],
                            pages_visited=[],
                            contact=contact,
                        )
                        try:
                            persisted = persist_fn(failed)
                            if inspect.isawaitable(persisted):
                                await persisted
                        except Exception as persist_error:
                            log.error(
                                "Could not persist failed person record (%s)",
                                type(persist_error).__name__,
                            )

    await asyncio.gather(*(run_company(group) for group in groups.values()))
    return [results[index] for index in sorted(results)]


def _completed_contacts(settings: Settings) -> set[tuple[str, str, str]]:
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        rows = conn.execute(
            "SELECT p.first_name,p.last_name,c.domain FROM person p "
            "JOIN company c ON c.id=p.company_id WHERE p.status IN ('found','not_found')"
        ).fetchall()
        return {
            (
                str(row["first_name"]).casefold(),
                str(row["last_name"]).casefold(),
                str(row["domain"]).casefold(),
            )
            for row in rows
        }
    finally:
        conn.close()


def _contact_key(contact: Contact) -> tuple[str, str, str]:
    return (
        contact.first_name.casefold(),
        contact.last_name.casefold(),
        contact.domain.casefold(),
    )


def _established_pattern(settings: Settings, domain: str) -> str | None:
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        row = conn.execute(
            "SELECT pattern FROM domain_pattern WHERE domain=? COLLATE NOCASE",
            (domain.strip().lower().rstrip("."),),
        ).fetchone()
        return row["pattern"] if row and is_established(conn, domain) else None
    finally:
        conn.close()


def _as_enrichment(walked: WalkResult, settings: Settings) -> EnrichmentResult:
    candidates = walked.candidates
    best = max(
        candidates,
        key=lambda item: (is_sendable(item, settings), item.confidence),
        default=None,
    )
    contact = walked.contact or (candidates[0].contact if candidates else None)
    if contact is None:
        raise ValueError("WalkResult is missing its contact")
    return EnrichmentResult(
        contact=contact,
        best_email=best,
        all_candidates=candidates,
        status=walked.status,
        stop_reason=walked.stop_reason,
        fetches_used=len(walked.pages_visited),
        provenance=walked.pages_visited,
    )
