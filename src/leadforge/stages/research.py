"""Bounded public-web research for company roles, people, and email evidence.

The stage deliberately records a trajectory instead of pretending that a
single search result is proof. Search queries, result cards, fetched pages,
LLM decisions, extracted clues, and the final stop reason are persisted.
All network access still goes through the normal policy router and no mail is
sent by this module.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.job_intel import extract_salary
from leadforge.models import PersonCandidate
from leadforge.sources.domains import is_synthetic_domain
from leadforge.sources.people import _is_probable_name, extract_people
from leadforge.sources.search import (
    DuckDuckGoProvider,
    SearxngProvider,
    parse_person_snippet,
)
from leadforge.stages.people_e2e import _split_name


class ResearchDecision(BaseModel):
    queries: list[str] = Field(default_factory=list)
    stop: bool = False
    reasoning: str = ""


@dataclass(frozen=True)
class ResearchBudget:
    max_queries: int = 8
    max_hits: int = 60
    max_fetches: int = 16
    max_llm_tokens: int = 20_000
    wall_clock_seconds: int = 300


@dataclass
class ResearchResult:
    run_id: str
    company: str
    domain: str
    people: list[PersonCandidate] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    jobs: list[dict[str, str]] = field(default_factory=list)
    queries_used: int = 0
    hits_seen: int = 0
    pages_fetched: int = 0
    llm_tokens: int = 0
    stop_reason: str = ""


def _tokens(value: object) -> int:
    return max(1, len(str(value)) // 4)


def _emails(text: str) -> list[str]:
    return sorted(
        {
            value.lower()
            for value in re.findall(
                r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.I
            )
            if not value.lower().endswith((".png", ".jpg", ".jpeg"))
            and value.split("@", 1)[0].lower()
            not in {"info", "hello", "contact", "careers", "jobs", "hr"}
        }
    )


def _queries(company: str, role: str, domain: str) -> list[str]:
    role_part = role or "hiring manager recruiter talent acquisition"
    return [
        f'site:linkedin.com/in "{company}" "{role_part}"',
        f'site:glassdoor.com "{company}" "{role_part}"',
        f'"{company}" "{role_part}" contact',
        f'"{company}" "{role_part}" (recruiter OR "talent acquisition" OR founder)',
        f'"{company}" "{role_part}" email',
        f'"{company}" "{role_part}" (salary OR compensation OR pay)',
        f'site:{domain} (team OR people OR leadership OR contact) "{role_part}"',
    ]


def _provider(settings: Settings, fetcher):
    if getattr(settings, "searxng_base_url", None):
        return "searxng", SearxngProvider(settings.searxng_base_url, fetcher=fetcher)
    return "ddg", DuckDuckGoProvider(fetcher=fetcher)


def _event(
    conn,
    run_id: str,
    sequence: int,
    kind: str,
    *,
    provider: str = "",
    query: str = "",
    url: str = "",
    parent_url: str = "",
    rank: int | None = None,
    payload: dict | None = None,
) -> None:
    conn.execute(
        """INSERT INTO research_event
           (run_id, sequence, kind, provider, query, url, parent_url, rank,
            payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            sequence,
            kind,
            provider,
            query,
            url,
            parent_url,
            rank,
            json.dumps(payload or {}, sort_keys=True),
            clock.utcnow_iso(),
        ),
    )


def _edge(
    conn,
    run_id: str,
    *,
    from_kind: str,
    from_value: str,
    to_kind: str,
    to_value: str,
    reason: str,
    query: str = "",
    url: str = "",
) -> None:
    conn.execute(
        """INSERT INTO research_edge
           (run_id, from_kind, from_value, to_kind, to_value, reason, query, url, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            from_kind,
            from_value,
            to_kind,
            to_value,
            reason,
            query,
            url,
            clock.utcnow_iso(),
        ),
    )


def _upsert_person(
    conn, company_id: int, candidate: PersonCandidate, profile_id: str
) -> int | None:
    from leadforge.sources.people import _has_role_keyword

    if not _is_probable_name(candidate.name) or not candidate.title:
        return None
    if not _has_role_keyword(candidate.title):
        return None
    first, last = _split_name(candidate.name)
    row = conn.execute(
        "SELECT id FROM person WHERE company_id=? AND first_name=? AND last_name=?",
        (company_id, first, last),
    ).fetchone()
    now = clock.utcnow_iso()
    if row:
        conn.execute(
            "UPDATE person SET title=COALESCE(NULLIF(?, ''), title), updated_at=? WHERE id=?",
            (candidate.title, now, int(row["id"])),
        )
        return int(row["id"])
    cur = conn.execute(
        """INSERT INTO person(company_id, first_name, last_name, title, role_class,
           status, created_at, updated_at, profile_id)
           VALUES (?, ?, ?, ?, 'other', 'new', ?, ?, ?)""",
        (company_id, first, last, candidate.title, now, now, profile_id),
    )
    return int(cur.lastrowid)


async def research_company(
    settings: Settings,
    company: str,
    domain: str,
    *,
    role: str = "",
    profile_id: str = "ai_ml",
    budget: ResearchBudget | None = None,
    fetcher=None,
    llm_client=None,
    dry_run: bool = False,
    progress: Callable[[str, dict[str, object]], None] | None = None,
) -> ResearchResult:
    """Research one company until a supported person/email is found or budget ends."""

    from leadforge.web.fetcher import Fetcher

    budget = budget or ResearchBudget()
    domain = domain.strip().lower()
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    result = ResearchResult(run_id=run_id, company=company, domain=domain)
    if progress:
        progress("research_start", {"company": company, "domain": domain, "run_id": run_id})
    if dry_run:
        result.stop_reason = "dry_run"
        if progress:
            progress("research_stop", {"reason": result.stop_reason})
        return result
    fetcher = fetcher or Fetcher(settings)
    provider_name, provider = _provider(settings, fetcher)
    conn = connect(settings.db_path)
    init_schema(conn)
    company_row = conn.execute("SELECT id FROM company WHERE domain=?", (domain,)).fetchone()
    if company_row is None:
        now = clock.utcnow_iso()
        company_id = int(
            conn.execute(
                "INSERT INTO company(name, domain, source, created_at) VALUES (?, ?, ?, ?)",
                (company, domain, "research", now),
            ).lastrowid
        )
    else:
        company_id = int(company_row["id"])
    conn.execute(
        """INSERT INTO research_run(id, company_id, profile_id, started_at)
           VALUES (?, ?, ?, ?)""",
        (run_id, company_id, profile_id, clock.utcnow_iso()),
    )
    conn.commit()
    sequence = 0
    queue = _queries(company, role, domain)
    seen_queries: set[str] = set()
    seen_urls: set[str] = set()
    people_by_name: dict[str, PersonCandidate] = {}
    person_emails: dict[str, dict[str, set[str]]] = {}
    job_rows = conn.execute(
        """SELECT title, url, location, employment_type, description,
                  salary_min, salary_max, salary_currency, salary_period, source
           FROM job WHERE company_id=? ORDER BY last_seen DESC""",
        (company_id,),
    ).fetchall()
    result.jobs = [
        {
            key: str(row[key] or "")
            for key in (
                "title", "url", "location", "employment_type", "description",
                "salary_min", "salary_max", "salary_currency", "salary_period", "source",
            )
        }
        for row in job_rows
    ]
    if result.jobs:
        sequence = 1
        _event(
            conn,
            run_id,
            sequence,
            "job_context",
            payload={"jobs": result.jobs[:20]},
        )

    async def llm_queries(context: str) -> list[str]:
        if llm_client is False or result.llm_tokens >= budget.max_llm_tokens:
            return []
        try:
            from leadforge.llm import chat_json

            response = await (llm_client.chat_json(
                "Generate safe public-web search queries for finding a real person and "
                "published work email at the named company. Never invent URLs.",
                context,
                schema=ResearchDecision,
            ) if llm_client is not None else chat_json(
                "Generate safe public-web search queries for finding a real person and "
                "published work email at the named company. Never invent URLs.",
                context,
                schema=ResearchDecision,
            ))
            result.llm_tokens += _tokens(response)
            if result.llm_tokens > budget.max_llm_tokens:
                result.llm_tokens = budget.max_llm_tokens
            return [
                q.strip() for q in response.queries
                if q.strip() and len(q) <= 300 and q.strip() not in seen_queries
            ][:3]
        except Exception:
            return []

    try:
        while queue and result.queries_used < budget.max_queries:
            if time.monotonic() - started >= budget.wall_clock_seconds:
                result.stop_reason = "wall_clock_budget"
                break
            if result.hits_seen >= budget.max_hits or result.pages_fetched >= budget.max_fetches:
                result.stop_reason = "research_budget"
                break
            query = queue.pop(0)
            if query in seen_queries:
                continue
            seen_queries.add(query)
            result.queries_used += 1
            if progress:
                progress(
                    "query",
                    {
                        "query": query,
                        "number": result.queries_used,
                        "budget": budget.max_queries,
                    },
                )
            sequence += 1
            _event(conn, run_id, sequence, "query", provider=provider_name, query=query)
            try:
                hits = await provider.search(query)
            except Exception as exc:
                _event(conn, run_id, sequence + 1, "search_error", provider=provider_name,
                       query=query, payload={"error": f"{type(exc).__name__}: {exc}"})
                sequence += 1
                continue
            for rank, hit in enumerate(hits, 1):
                if result.hits_seen >= budget.max_hits:
                    break
                result.hits_seen += 1
                if progress:
                    progress(
                        "search_hit",
                        {
                            "rank": rank,
                            "title": hit.title,
                            "url": hit.url,
                            "snippet": hit.snippet,
                            "number": result.hits_seen,
                            "budget": budget.max_hits,
                        },
                    )
                sequence += 1
                _event(
                    conn, run_id, sequence, "search_hit", provider=provider_name,
                    query=query, url=hit.url, rank=rank,
                    payload={"title": hit.title, "snippet": hit.snippet},
                )
                _edge(
                    conn,
                    run_id,
                    from_kind="search_query",
                    from_value=query,
                    to_kind="page",
                    to_value=hit.url,
                    reason="followed_relevant_search_result",
                    query=query,
                    url=hit.url,
                )
                parsed = parse_person_snippet(hit, expected_company=company)
                if parsed and parsed.name.lower() not in people_by_name:
                    people_by_name[parsed.name.lower()] = parsed
                    if progress:
                        progress(
                            "person_clue",
                            {"name": parsed.name, "title": parsed.title, "url": hit.url},
                        )
                    sequence += 1
                    _event(
                        conn,
                        run_id,
                        sequence,
                        "person_clue",
                        provider=provider_name,
                        query=query,
                        url=hit.url,
                        rank=rank,
                        payload={"name": parsed.name, "title": parsed.title, "source": "snippet"},
                    )
                hit_text = f"{hit.title} {hit.snippet}"
                for email in _emails(hit_text):
                    result.emails.append(email) if email not in result.emails else None
                    if parsed and parsed.name.lower() in hit_text.lower():
                        person_emails.setdefault(parsed.name.lower(), {}).setdefault(
                            email, set()
                        ).add(hit.url)
                        _edge(
                            conn,
                            run_id,
                            from_kind="person",
                            from_value=parsed.name,
                            to_kind="email",
                            to_value=email,
                            reason="email_appeared_in_same_search_result",
                            query=query,
                            url=hit.url,
                        )
                        if progress:
                            progress(
                                "email_clue",
                                {"email": email, "person": parsed.name, "url": hit.url},
                            )
                        sequence += 1
                        _event(
                            conn,
                            run_id,
                            sequence,
                            "email_clue",
                            provider=provider_name,
                            query=query,
                            url=hit.url,
                            rank=rank,
                            payload={"email": email, "person": parsed.name, "source": "snippet"},
                        )
                if hit.url in seen_urls or result.pages_fetched >= budget.max_fetches:
                    continue
                parsed_url = urlparse(hit.url)
                if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                    continue
                seen_urls.add(hit.url)
                try:
                    page = await fetcher.fetch(hit.url, purpose="person")
                except Exception as exc:
                    sequence += 1
                    _event(conn, run_id, sequence, "fetch_error", url=hit.url,
                           parent_url=query, payload={"error": f"{type(exc).__name__}: {exc}"})
                    continue
                result.pages_fetched += 1
                if progress:
                    progress(
                        "fetch",
                        {
                            "url": hit.url,
                            "number": result.pages_fetched,
                            "budget": budget.max_fetches,
                        },
                    )
                html = page.html or ""
                sequence += 1
                _event(conn, run_id, sequence, "fetch", url=hit.url, parent_url=query,
                       payload={"chars": len(html)})
                _edge(
                    conn,
                    run_id,
                    from_kind="page",
                    from_value=hit.url,
                    to_kind="page",
                    to_value=getattr(page, "final_url", None) or hit.url,
                    reason="followed_redirect_or_rendered_page",
                    query=query,
                    url=hit.url,
                )
                for candidate in extract_people(html, hit.url, company):
                    if candidate.name.lower() not in people_by_name:
                        people_by_name[candidate.name.lower()] = candidate
                        if progress:
                            progress(
                                "person_clue",
                                {"name": candidate.name, "title": candidate.title, "url": hit.url},
                            )
                        sequence += 1
                        _event(
                            conn,
                            run_id,
                            sequence,
                            "person_clue",
                            url=hit.url,
                            parent_url=query,
                            payload={
                                "name": candidate.name,
                                "title": candidate.title,
                                "source": "page",
                            },
                        )
                page_emails = _emails(html)
                result.emails.extend(email for email in page_emails if email not in result.emails)
                for candidate in people_by_name.values():
                    if candidate.name.lower() in html.lower():
                        email_map = person_emails.setdefault(candidate.name.lower(), {})
                        for email in page_emails:
                            email_map.setdefault(email, set()).add(hit.url)
                            _edge(
                                conn,
                                run_id,
                                from_kind="person",
                                from_value=candidate.name,
                                to_kind="email",
                                to_value=email,
                                reason="email_appeared_on_page_containing_person_name",
                                query=query,
                                url=hit.url,
                            )
                        for email in page_emails:
                            if progress:
                                progress(
                                    "email_clue",
                                    {"email": email, "person": candidate.name, "url": hit.url},
                                )
                            sequence += 1
                            _event(
                                conn,
                                run_id,
                                sequence,
                                "email_clue",
                                url=hit.url,
                                parent_url=query,
                                payload={
                                    "email": email,
                                    "person": candidate.name,
                                    "source": "page",
                                },
                            )
            salary_query = (
                "salary" in query.lower()
                or "compensation" in query.lower()
                or "pay" in query.lower()
            )
            if salary_query:
                salary = extract_salary(" ".join(f"{hit.title} {hit.snippet}" for hit in hits))
                if salary:
                    target = next(
                        (
                            row
                            for row in job_rows
                            if any(
                                token.lower() in (row["title"] or "").lower()
                                for token in role.split()
                                if len(token) > 3
                            )
                        ),
                        job_rows[0] if job_rows else None,
                    )
                    if target:
                        now = clock.utcnow_iso()
                        conn.execute(
                            """UPDATE job SET salary_min=?, salary_max=?, salary_currency=?,
                               salary_period=?, salary_source=?, salary_raw=?,
                               salary_evidence_url=?, salary_retrieved_at=?,
                               salary_extraction_method=?, salary_search_status=?
                               WHERE company_id=? AND title=?""",
                            (
                                salary.min_amount,
                                salary.max_amount,
                                salary.currency,
                                salary.period,
                                "research-search",
                                salary.raw,
                                next((hit.url for hit in hits if extract_salary(
                                    f"{hit.title} {hit.snippet}"
                                )), ""),
                                now,
                                "search_snippet",
                                "searched_salary_query",
                                company_id,
                                target["title"],
                            ),
                        )
                    sequence += 1
                    _event(
                        conn,
                        run_id,
                        sequence,
                        "salary_clue",
                        query=query,
                        payload={
                            "raw": salary.raw,
                            "min": salary.min_amount,
                            "max": salary.max_amount,
                            "currency": salary.currency,
                            "period": salary.period,
                        },
                    )
                    if progress:
                        progress(
                            "salary_clue",
                            {
                                "raw": salary.raw,
                                "currency": salary.currency,
                                "period": salary.period,
                            },
                        )
            if people_by_name:
                context = "\n".join(
                    f"{h.title} | {h.snippet}" for h in hits[:10]
                )
                queue.extend(await llm_queries(
                    f"COMPANY: {company}\nROLE: {role}\nCURRENT EVIDENCE:\n{context}"
                ))
            if people_by_name and any(person_emails.values()):
                result.stop_reason = "person_and_email_found"
                break
        if not result.stop_reason:
            result.stop_reason = (
                "query_budget"
                if result.queries_used >= budget.max_queries
                else "no_more_queries"
            )
    # NEW: Agentic fallback - if no people found via snippets, use LLM-guided crawl
    if not people_by_name and not dry_run:
        from leadforge.sources.people_agent import find_people_agentic
        try:
            agentic_result = await find_people_agentic(
                company=company,
                start_url=f"https://{domain}/about",
                goal="find HR, talent acquisition, or engineering managers",
                max_pages=5,
                max_tokens=15_000,
            )
            for person in agentic_result.people:
                if person.name.lower() not in people_by_name:
                    people_by_name[person.name.lower()] = person
            result.stop_reason += f"; agentic_found={len(agentic_result.people)}"
        except Exception as exc:
            result.stop_reason += f"; agentic_error={type(exc).__name__}"

    finally:
        for candidate in people_by_name.values():
            result.people.append(candidate)
            if not dry_run:
                person_id = _upsert_person(conn, company_id, candidate, profile_id)
                if person_id:
                    for email, urls in sorted(
                        person_emails.get(candidate.name.lower(), {}).items()
                    ):
                        if email.endswith("@" + domain):
                            conn.execute(
                                """INSERT INTO evidence(person_id, company_id, kind, value,
                                   professional, created_at) VALUES (?, ?, 'research_email',
                                   ?, 1, ?)""",
                                (person_id, company_id, email, clock.utcnow_iso()),
                            )
                            conn.execute(
                                """INSERT INTO email_candidate
                                   (person_id, email, source, evidence_tier, found_on_web,
                                    verification_method, verification_verdict, confidence, notes,
                                    created_at)
                                   VALUES (?, ?, 'research-web', 'C', 1, 'unverified',
                                           'unknown', 0.25, ?, ?) ON CONFLICT(person_id,email)
                                   DO UPDATE SET found_on_web=1, notes=excluded.notes""",
                                (
                                    person_id,
                                    email,
                                    f"run={run_id};urls={','.join(sorted(urls))}",
                                    clock.utcnow_iso(),
                                ),
                            )
        sequence += 1
        _event(conn, run_id, sequence, "stop", payload={
            "reason": result.stop_reason,
            "queries": result.queries_used,
            "hits": result.hits_seen,
            "fetches": result.pages_fetched,
            "llm_tokens": result.llm_tokens,
        })
        conn.execute(
            """UPDATE research_run SET finished_at=?, stop_reason=?, queries_used=?,
               hits_seen=?, pages_fetched=?, llm_tokens=? WHERE id=?""",
            (clock.utcnow_iso(), result.stop_reason, result.queries_used, result.hits_seen,
             result.pages_fetched, result.llm_tokens, run_id),
        )
        conn.commit()
        conn.close()
        if progress:
            progress(
                "research_stop",
                {
                    "reason": result.stop_reason,
                    "queries": result.queries_used,
                    "hits": result.hits_seen,
                    "fetches": result.pages_fetched,
                    "people": len(result.people),
                    "emails": len(result.emails),
                },
            )
    return result


async def run_research(
    settings: Settings,
    *,
    profile_id: str = "ai_ml",
    max_companies: int | None = 10,
    budget: ResearchBudget | None = None,
    dry_run: bool = False,
    progress: Callable[[str, dict[str, object]], None] | None = None,
) -> list[ResearchResult]:
    """Run bounded research for companies represented by the job table."""
    conn = connect(settings.db_path)
    init_schema(conn)
    rows = conn.execute(
        """SELECT DISTINCT c.name, c.domain
           FROM company c JOIN job j ON j.company_id=c.id
           WHERE (j.profile_id=? OR j.profile_id IS NULL)
           ORDER BY c.name""",
        (profile_id,),
    ).fetchall()
    conn.close()
    if max_companies is not None:
        rows = rows[:max_companies]
    results: list[ResearchResult] = []
    for row in rows:
        domain = (row["domain"] or "").strip().lower()
        if not domain or is_synthetic_domain(domain):
            continue
        results.append(
            await research_company(
                settings,
                row["name"] or domain,
                domain,
                profile_id=profile_id,
                budget=budget,
                dry_run=dry_run,
                progress=progress,
            )
        )
    return results
