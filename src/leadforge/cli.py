import asyncio
import csv
import functools
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import dns.resolver
import typer
from rich.console import Console
from rich.table import Table

from leadforge import __version__
from leadforge.config import setting_env_name, setting_env_names, settings
from leadforge.db import connect, init_schema, schema_version
from leadforge.draft_quality import quality_issues
from leadforge.llm import LLMNotConfigured, LLMOutputError, chat_json
from leadforge.llm.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from leadforge.llm.schemas import PingResponse
from leadforge.models import Contact, EmailCandidate, OperatorProfile
from leadforge.outreach import daily_cap, is_suppressed_or_contacted, sent_today
from leadforge.scoring import is_sendable
from leadforge.sources.search import (
    DuckDuckGoProvider,
    ReplaySearchProvider,
    SearchProvider,
    SearxngProvider,
    dedupe_people,
    parse_person_snippet,
    person_query,
)
from leadforge.stages.discovery import (
    discover_company_jobs,
    discover_hn_signals,
    persist_invite_signals,
    plan_discovery,
    read_companies,
    save_jobs,
)
from leadforge.stages.import_pages import parse_saved_page, persist_import, saved_page_paths
from leadforge.stages.pattern_learn import is_established, record_pattern_sample
from leadforge.stages.profile import extract_profile, read_resume, save_profile
from leadforge.stages.replay import ReplayFetcher
from leadforge.stages.seed_companies import seed_sync
from leadforge.stages.walk import FetchFn as WalkFetchFn
from leadforge.stages.walk import WalkBudget, WalkResult, seed_urls, walk_person
from leadforge.tools.smtp_verify import (
    Port25Status,
    check_port25_status,
    smtp_mail_from_problem,
    verify_candidates,
)
from leadforge.web.denylist import load_denylist
from leadforge.web.errors import FetchError
from leadforge.web.fetcher import Fetcher
from leadforge.web.policy import DomainPolicy
from leadforge.web.tiers.http import tier1_backend

app = typer.Typer(
    name="leadforge",
    help="Self-hosted email enrichment pipeline.",
    no_args_is_help=True,
)
profile_app = typer.Typer(help="Inspect and edit operator profile inputs.")
app.add_typer(profile_app, name="profile")
domains_app = typer.Typer(help="Manage host pauses.")
app.add_typer(domains_app, name="domains")
console = Console()


@app.command()
def version() -> None:
    """Print the current version."""
    console.print(f"leadforge [bold cyan]{__version__}[/]")


@app.command()
def doctor() -> None:
    """Check Python, dependencies, and local configuration."""
    console.print(f"[bold]Python:[/] {sys.version.split()[0]}")
    console.print("[bold]Environments:[/] operator uses .\\.venv; agent uses .\\.venv-agent")
    console.print(f"[bold]Data dir:[/] {settings.data_dir.resolve()}")
    console.print(f"[bold]SMTP verify mode:[/] {settings.smtp_verify_mode}")
    mail_from_name = setting_env_name("smtp_mail_from")
    console.print(f"[bold]SMTP MAIL FROM variable:[/] {mail_from_name or '(not set)'}")
    if settings.smtp_verify_mode == "domain":
        mail_from_problem = smtp_mail_from_problem(settings.smtp_mail_from)
        if mail_from_problem:
            console.print(f"[bold yellow]Warning:[/] {mail_from_problem}")
    dns_nameservers = settings.dns_nameservers or dns.resolver.get_default_resolver().nameservers
    dns_source = "configured" if settings.dns_nameservers else "system"
    dns_names = setting_env_name("dns_nameservers")
    console.print(
        f"[bold]DNS resolver (as seen from this process):[/] "
        f"{', '.join(map(str, dns_nameservers))} ({dns_source}; "
        f"variable={dns_names or '(not set)'})"
    )
    console.print(f"[bold]Min confidence:[/] {settings.min_confidence_to_emit}")
    for field_name, label in (
        ("llm_base_url", "LLM base URL variable"),
        ("llm_api_key", "LLM API key variable"),
        ("llm_model", "LLM model variable"),
        ("obscura_path", "Obscura path variable"),
    ):
        console.print(f"[bold]{label}:[/] {setting_env_name(field_name) or '(not set)'}")
    for field_name in (
        "smtp_mail_from",
        "dns_nameservers",
        "obscura_path",
        "obscura_stealth",
        "obscura_timeout_seconds",
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_max_input_chars",
        "llm_timeout_seconds",
        "llm_requests_per_minute",
        "llm_daily_cap",
    ):
        names = setting_env_names(field_name)
        legacy = [name for name in names if not name.startswith("LEADFORGE_")]
        preferred = [name for name in names if name.startswith("LEADFORGE_")]
        if legacy:
            console.print(
                f"[bold yellow]Warning:[/] legacy environment variable name supplied: "
                f"{', '.join(legacy)}"
            )
        if legacy and preferred:
            console.print(
                f"[bold yellow]Warning:[/] both legacy and prefixed names are set for "
                f"{field_name}: {', '.join((*legacy, *preferred))}"
            )
    policy = DomainPolicy.load(settings.domain_policy_path, settings)
    console.print(f"[bold]Tier 1 backend:[/] {tier1_backend()}")
    obscura = settings.obscura_path
    if obscura and obscura.is_file():
        console.print(f"[bold]Obscura:[/] {_obscura_version(obscura)} (configured)")
        console.print("[bold]Tier 2:[/] enabled")
    else:
        bundled_obscura = Path("bin/obscura/obscura.exe")
        if bundled_obscura.is_file():
            console.print(
                f"[bold]Obscura binary:[/] {_obscura_version(bundled_obscura)} "
                f"(not configured: {bundled_obscura.resolve()})"
            )
        console.print("[bold]Tier 2:[/] disabled (OBSCURA_PATH not set)")
    try:
        from scrapling.fetchers import Fetcher as _ScraplingFetcher  # noqa: F401

        scrapling_available = True
    except ImportError:
        scrapling_available = False
    console.print(f"[bold]Scrapling importable:[/] {'yes' if scrapling_available else 'no'}")
    console.print(
        f"[bold]Domain policy:[/] "
        f"{'loaded' if settings.domain_policy_path.exists() else 'missing'} "
        f"({len(policy.rules)} host rules)"
    )
    denylist_count = len(load_denylist(settings.denylist_hosts_path))
    console.print(f"[bold]Denylist:[/] {denylist_count} hosts")
    _show_platform_budgets()
    try:
        port25_status = asyncio.run(check_port25_status(timeout_seconds=5.0))
    except Exception as exc:
        console.print(f"[bold]Port 25 (as seen from this process):[/] check failed ({exc})")
    else:
        console.print(f"[bold]Port 25 (as seen from this process):[/] {port25_status.value}")
        if port25_status is Port25Status.DNS_FAILED:
            console.print(
                f"  Resolver in use: {', '.join(map(str, dns_nameservers))} ({dns_source})"
            )
            console.print(
                "  Remedy: Check DNS resolution and network egress, then retry leadforge doctor."
            )
        elif port25_status is Port25Status.BLOCKED:
            console.print(
                "  Remedy: Run SMTP verification on a network that permits outbound port 25."
            )
        else:
            console.print("  Remedy: No action needed; outbound port 25 is reachable.")
    console.print()


def _show_platform_budgets() -> None:
    try:
        conn = connect(settings.db_path)
        rows = conn.execute("SELECT host, count_today, day FROM domain_state").fetchall()
        conn.close()
    except Exception:
        rows = []
    platforms = ("linkedin.com", "indeed.com", "glassdoor.com")
    today = datetime.now(UTC).date().isoformat()
    console.print("[bold]Daily platform budget used:[/]")
    for platform in platforms:
        used = sum(
            int(row["count_today"])
            for row in rows
            if row["day"] == today
            and (row["host"] == platform or row["host"].endswith("." + platform))
        )
        rule = DomainPolicy.load(settings.domain_policy_path, settings).resolve(platform)
        console.print(f"  {platform}: {used}/{rule.daily_cap}")


def _obscura_version(executable: Path) -> str:
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "version unavailable"
    version_text = completed.stdout.strip() or completed.stderr.strip()
    return version_text or "version unavailable"


@app.command()
def fetch(
    url: str,
    tier: int = typer.Option(1, min=1, max=3),
    no_cache: bool = typer.Option(False, "--no-cache"),
    text: bool = typer.Option(False, "--text"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Fetch one URL through the router; --dry-run never makes a request."""
    if dry_run:
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname or "(invalid URL)"
        rule = DomainPolicy.load(settings.domain_policy_path, settings).resolve(host)
        console.print(
            f"Would fetch {url} through policy: enabled={rule.enabled}, "
            f"daily_cap={rule.daily_cap}, max_tier={rule.max_tier}; no request made"
        )
        return
    try:
        result = asyncio.run(Fetcher(settings).fetch(url, min_tier=tier, use_cache=not no_cache))
    except FetchError as exc:
        console.print(f"[red]Fetch failed:[/] {exc}")
        raise typer.Exit(code=1) from exc
    from leadforge.web.detect import classify_response

    classification = classify_response(result.status or 0, {}, result.html)
    console.print(
        f"status={result.status} tier={result.tier} classification={classification} "
        f"from_cache={result.from_cache}"
    )
    if text:
        from bs4 import BeautifulSoup

        cleaned = " ".join(BeautifulSoup(result.html, "lxml").get_text(" ").split())
        console.print(_terminal_safe_text(cleaned[:500]))


@app.command("import-pages")
def import_pages(
    dry_run: bool = typer.Option(False, "--dry-run"),
    delete_after: bool = typer.Option(False, "--delete-after"),
) -> None:
    """Import operator-saved HTML/MHTML/text pages without making network calls."""
    directory = settings.input_dir / "saved_pages"
    if dry_run:
        replay_settings = _replay_settings(settings)
        replay_db = connect(replay_settings.db_path)
        try:
            init_schema(replay_db)
        finally:
            replay_db.close()
    pages = [parse_saved_page(path) for path in saved_page_paths(directory)]
    if not pages:
        console.print(f"No saved pages found in {directory}")
        return
    recognized = [page for page in pages if page.status == "recognized"]
    for page in pages:
        console.print(f"{page.path.name}: {page.kind} — {page.status}")
        for field_name, value in page.evidence:
            console.print(f"  {field_name}: {value}")
    if dry_run:
        console.print(
            f"Would import {len(recognized)} of {len(pages)} saved pages; "
            "no files or records changed"
        )
        return
    for page in recognized:
        persist_import(page, settings)
        if delete_after:
            page.path.unlink()
    console.print(f"Imported {len(recognized)} of {len(pages)} saved pages")


@app.command("seed-companies")
def seed_companies(
    target: int = typer.Option(25, "--target", min=8, max=25),
    dry_run: bool = typer.Option(False, "--dry-run"),
    from_cache: bool = typer.Option(False, "--from-cache"),
) -> None:
    """Seed matching companies from public hiring sources or the local page cache."""
    try:
        companies, stats = seed_sync(target=target, dry_run=dry_run, from_cache=from_cache)
    except Exception as exc:
        console.print(f"[red]Seed failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc
    if dry_run:
        return
    if len(companies) < 8:
        console.print("STOP: fewer than 8 companies passed the acceptance checks")
        for source, item in stats.items():
            console.print(
                f"  {source}: pages={item.pages}, companies={item.companies}, "
                f"blocks={item.blocks}, robots_refusals={item.robots_refusals}"
            )
        return
    console.print(
        f"Seeded {len(companies)} accepted companies (target {target}); "
        "company rows and matching jobs were written"
    )
    for source, item in stats.items():
        console.print(
            f"  {source}: pages={item.pages}, companies={item.companies}, "
            f"blocks={item.blocks}, robots_refusals={item.robots_refusals}, "
            f"unrecognised={item.unrecognised}"
        )


@profile_app.command("ingest")
def profile_ingest(resume: Path) -> None:
    """Extract a deterministic, editable profile from a resume document."""
    if not resume.is_file():
        console.print(f"[red]Resume not found:[/] {resume}")
        raise typer.Exit(code=2)
    profile = extract_profile(read_resume(resume))
    output_path = settings.data_dir / "profile.json"
    save_profile(profile, output_path)
    console.print(output_path.read_text(encoding="utf-8"))


@app.command("discover")
def discover(
    companies: Annotated[Path, typer.Option(..., "--companies", exists=True, dir_okay=False)],
    dry_run: bool = typer.Option(False, "--dry-run"),
    include_hn: bool = typer.Option(False, "--include-hn"),
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
) -> None:
    """Find public jobs for operator-supplied companies; dry-run makes no requests."""
    company_rows = read_companies(companies)
    if limit is not None:
        company_rows = company_rows[:limit]
    if dry_run:
        console.print("Discovery plan (no requests made):")
        for item in plan_discovery(company_rows, settings):
            console.print(f"  {item}")
        if include_hn:
            rule = DomainPolicy.load(settings.domain_policy_path, settings).resolve(
                "hacker-news.firebaseio.com"
            )
            console.print(
                f"  https://hacker-news.firebaseio.com/v0/user/whoishiring.json "
                f"cap={rule.daily_cap} enabled={rule.enabled}"
            )
        return
    _require_operator_inputs()
    if companies.resolve() != (settings.input_dir / "companies.csv").resolve():
        console.print("[red]Discovery refused:[/] live runs require data/input/companies.csv.")
        raise typer.Exit(code=2)
    profile_path = settings.data_dir / "profile.json"
    profile = OperatorProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
    fetcher = Fetcher(settings)

    async def run_discovery():
        collected = []
        for company in company_rows:
            collected.extend(await discover_company_jobs(company, profile, fetcher=fetcher))
        if include_hn:
            collected.extend(await discover_hn_signals(company_rows, profile))
        return collected

    jobs = asyncio.run(run_discovery())
    persist_invite_signals(jobs, settings)
    output_path = settings.output_dir / "discovered_jobs.csv"
    save_jobs(jobs, output_path)
    console.print(f"Discovered {len(jobs)} jobs → {output_path}")


@app.command("people")
def people(
    company: str = typer.Option(..., "--company"),
    domain: str = typer.Option(..., "--domain"),
    max_people: Annotated[int, typer.Option("--max", min=1, max=50)] = 5,
    replay: Annotated[Path | None, typer.Option("--replay", exists=True, file_okay=False)] = None,
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Find candidate names from search snippets; result URLs are never fetched."""
    query = person_query(company, domain, settings)
    if dry_run:
        host = (
            "replay fixture"
            if replay
            else settings.searxng_base_url or "no live provider configured"
        )
        console.print(f"Would search {host} for: {query}; no request made")
        return
    provider: SearchProvider
    active_settings = _replay_settings(settings) if replay is not None else settings
    if replay is not None:
        provider = ReplaySearchProvider(replay)
    else:
        companies_path = settings.input_dir / "companies.csv"
        profile_path = settings.data_dir / "profile.json"
        if not companies_path.is_file() or not profile_path.is_file():
            console.print(
                "[red]Live people search refused:[/] provide data/input/companies.csv "
                "and ingest a resume first."
            )
            raise typer.Exit(code=2)
        fetcher = Fetcher(settings)
        provider = (
            SearxngProvider(settings.searxng_base_url, fetcher=fetcher)
            if settings.searxng_base_url
            else DuckDuckGoProvider(fetcher=fetcher)
        )
    hits = asyncio.run(provider.search(query))
    candidates = dedupe_people(
        [candidate for hit in hits if (candidate := parse_person_snippet(hit, company))]
    )[:max_people]
    _persist_people(candidates, company, domain, active_settings)
    table = Table("Name", "Title", "Company", "Source URL")
    for candidate in candidates:
        table.add_row(candidate.name, candidate.title, candidate.company, candidate.url)
    console.print(table)
    console.print(f"People candidates: {len(candidates)}")


def _persist_people(candidates, company: str, domain: str, active_settings) -> None:
    if not candidates:
        return
    now = datetime.now(UTC).isoformat()
    conn = connect(active_settings.db_path)
    try:
        init_schema(conn)
        company_row = conn.execute(
            "SELECT id FROM company WHERE lower(domain)=lower(?)", (domain,)
        ).fetchone()
        if company_row:
            company_id = int(company_row["id"])
        else:
            cursor = conn.execute(
                "INSERT INTO company(name,domain,source,created_at) VALUES(?,?,?,?)",
                (company, domain.lower(), "people_search", now),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("company insert did not return an id")
            company_id = cursor.lastrowid
        for candidate in candidates:
            parts = candidate.name.strip().split()
            if len(parts) < 2:
                continue
            first, last = parts[0], " ".join(parts[1:])
            person = conn.execute(
                "SELECT id FROM person WHERE company_id=? AND lower(first_name)=lower(?) "
                "AND lower(last_name)=lower(?) ORDER BY id LIMIT 1",
                (company_id, first, last),
            ).fetchone()
            if person:
                person_id = int(person["id"])
                conn.execute(
                    "UPDATE person SET title=?,updated_at=? WHERE id=?",
                    (candidate.title, now, person_id),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO person(company_id,first_name,last_name,title,status,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (company_id, first, last, candidate.title, "new", now, now),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("person insert did not return an id")
                person_id = cursor.lastrowid
            if candidate.snippet:
                conn.execute(
                    "INSERT INTO evidence(person_id,company_id,kind,value,professional,created_at) "
                    "SELECT ?,?,?,?,1,? WHERE NOT EXISTS (SELECT 1 FROM evidence WHERE "
                    "person_id=? AND kind='search_snippet' AND value=?)",
                    (
                        person_id,
                        company_id,
                        "search_snippet",
                        candidate.snippet,
                        now,
                        person_id,
                        candidate.snippet,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _terminal_safe_text(value: str, encoding: str | None = None) -> str:
    """Replace characters the active Windows terminal encoding cannot display."""
    terminal_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(terminal_encoding, errors="replace").decode(
        terminal_encoding, errors="replace"
    )


@app.command("llm-ping")
def llm_ping(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Check an explicitly configured OpenAI-compatible LLM endpoint."""
    if dry_run:
        console.print(f"Would ping model {settings.llm_model or '(unset)'}; no LLM request made")
        return
    try:
        result = asyncio.run(
            chat_json(
                "Return a JSON object with a boolean ok value.",
                'Reply with the JSON {"ok": true}',
                PingResponse,
                settings=settings,
                prompt_version=PROMPT_VERSION,
            )
        )
    except LLMNotConfigured:
        console.print("LLM not configured")
        return
    except LLMOutputError as exc:
        console.print(f"LLM ping failed: {exc}")
        raise typer.Exit(code=1) from exc
    console.print("LLM configured and responding" if result.ok else "LLM returned ok=false")


@app.command()
def verify(
    csv_path: Path,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Verify candidate addresses from a CSV without sending email."""
    if dry_run:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
            count = sum(1 for _ in csv.DictReader(source))
        console.print(f"Would check {count} candidate addresses; no SMTP requests made")
        return
    _require_smtp_mail_from()
    asyncio.run(_verify_csv(csv_path))


def _require_smtp_mail_from() -> None:
    if settings.smtp_verify_mode != "domain":
        return
    problem = smtp_mail_from_problem(settings.smtp_mail_from)
    if problem:
        console.print(f"[red]SMTP checks refused:[/] {problem}")
        raise typer.Exit(code=2)


@app.command("walk")
def walk_command(
    company_domain: str = typer.Option(..., "--company-domain"),
    first: str = typer.Option(..., "--first"),
    last: str = typer.Option(..., "--last"),
    title: str = typer.Option("", "--title"),
    replay: Annotated[Path | None, typer.Option("--replay", exists=True, file_okay=False)] = None,
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Run a bounded person walk; --replay serves fixture pages without network access."""
    contact = Contact(
        first_name=first,
        last_name=last,
        domain=company_domain,
        company=company_domain,
        title=title or None,
        source="walk",
    )
    if replay is not None:
        replay_fetcher = ReplayFetcher(replay)

        async def offline_llm(**kwargs):
            raise LLMNotConfigured("Replay mode uses deterministic link ranking")

        async def offline_verify(candidates):
            return candidates

        fetch_operation: WalkFetchFn = replay_fetcher
        llm_fn = offline_llm
        verify_fn = offline_verify
        starts = replay_fetcher.seeds or seed_urls(company_domain)
    else:
        if dry_run:
            console.print(
                f"Would walk {company_domain} from {', '.join(seed_urls(company_domain))}; "
                "no requests made"
            )
            return
        _require_operator_inputs()
        fetcher = Fetcher(settings)

        async def live_llm(**kwargs):
            from leadforge.llm.prompts import build_user_message
            from leadforge.llm.schemas import PageExtraction

            return await chat_json(
                SYSTEM_PROMPT,
                build_user_message(
                    person_name=contact.full_name,
                    company=contact.company or company_domain,
                    role=contact.title or "",
                    page_text=kwargs["page_text"],
                    candidate_links=kwargs["candidate_links"],
                ),
                PageExtraction,
                settings=settings,
                prompt_version=PROMPT_VERSION,
            )

        async def live_fetch(url: str):
            return await fetcher.fetch(url)

        fetch_operation = live_fetch

        llm_fn = live_llm

        async def verify_fn(candidates: list[EmailCandidate]):
            return await verify_candidates(contact, candidates, settings)

        starts = seed_urls(company_domain)
    try:
        result = asyncio.run(
            walk_person(
                person_id=0,
                contact=contact,
                fetch_fn=fetch_operation,
                llm_fn=llm_fn,
                verify_fn=verify_fn,
                budget=WalkBudget(
                    settings.walk_max_fetches,
                    settings.walk_max_depth,
                    settings.walk_stale_hops,
                    settings.walk_wall_clock_seconds,
                ),
                seed_urls_override=starts,
                denylist_hosts=load_denylist(settings.denylist_hosts_path),
                persist_fn=functools.partial(
                    _persist_walk_result,
                    active_settings=_replay_settings(settings) if replay is not None else settings,
                ),
                min_confidence_to_emit=settings.min_confidence_to_emit,
                established_pattern=(
                    None if replay is not None else _established_pattern(company_domain)
                ),
            )
        )
    except LLMNotConfigured:
        console.print("LLM not configured; heuristic walk unavailable for this page")
        raise typer.Exit(code=1) from None
    console.print(
        f"Walk {result.status}: stop={result.stop_reason}, "
        f"pages={len(result.pages_visited)}, candidates={len(result.candidates)}"
    )
    for candidate in result.candidates:
        console.print(
            f"  {candidate.email} tier={candidate.evidence_tier} "
            f"verdict={candidate.verification_verdict.value} confidence={candidate.confidence:.2f}"
        )


def _replay_settings(active_settings):
    """Return settings bound to the replay-only database."""
    return active_settings.model_copy(update={"db_path": active_settings.cache_dir / "replay.db"})


def _persist_walk_result(result: WalkResult, active_settings=None) -> None:
    """Upsert the person state and persist candidate/evidence provenance locally."""
    active_settings = active_settings or settings
    now = datetime.now(UTC).isoformat()
    conn = connect(active_settings.db_path)
    try:
        init_schema(conn)
        candidate = result.candidates[0] if result.candidates else None
        contact = result.contact or (candidate.contact if candidate else None)
        if contact is None:
            return
        company = conn.execute(
            "SELECT id FROM company WHERE domain=?", (contact.domain.lower(),)
        ).fetchone()
        if company:
            company_id = int(company["id"])
        else:
            cursor = conn.execute(
                "INSERT INTO company(name,domain,source,created_at) VALUES(?,?,?,?)",
                (contact.company or contact.domain, contact.domain.lower(), "walk", now),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("company insert did not return an id")
            company_id = cursor.lastrowid
        person = conn.execute(
            "SELECT id FROM person WHERE company_id=? AND first_name=? AND last_name=? "
            "ORDER BY id LIMIT 1",
            (company_id, contact.first_name, contact.last_name),
        ).fetchone()
        if person:
            person_id = int(person["id"])
            conn.execute(
                "UPDATE person SET title=?,status=?,stop_reason=?,updated_at=? WHERE id=?",
                (contact.title, result.status, result.stop_reason, now, person_id),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO person(company_id,first_name,last_name,title,status,stop_reason,
                   created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    company_id,
                    contact.first_name,
                    contact.last_name,
                    contact.title,
                    result.status,
                    result.stop_reason,
                    now,
                    now,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("person insert did not return an id")
            person_id = cursor.lastrowid
        result.person_id = person_id
        conn.execute(
            "INSERT INTO run_log(ts,stage,level,message) VALUES(?,?,?,?)",
            (
                now,
                "enrich",
                "info",
                f"person_id={person_id} status={result.status} stop={result.stop_reason} "
                f"fetches={len(result.pages_visited)}",
            ),
        )
        for kind, value, professional in result.evidence:
            conn.execute(
                """INSERT INTO evidence(person_id,company_id,kind,value,professional,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (person_id, company_id, kind, value, int(professional), now),
            )
        for item in result.candidates:
            if item.evidence_tier == "A":
                record_pattern_sample(conn, contact, item)
            conn.execute(
                """INSERT INTO email_candidate(person_id,email,pattern,source,evidence_tier,
                   found_on_web,catch_all_domain,verification_method,verification_verdict,
                   confidence,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,email) DO UPDATE SET pattern=excluded.pattern,
                   source=excluded.source,evidence_tier=excluded.evidence_tier,
                   found_on_web=excluded.found_on_web,catch_all_domain=excluded.catch_all_domain,
                   verification_method=excluded.verification_method,
                   verification_verdict=excluded.verification_verdict,
                   confidence=excluded.confidence,notes=excluded.notes,created_at=excluded.created_at""",
                (
                    person_id,
                    item.email,
                    item.pattern,
                    item.source,
                    item.evidence_tier,
                    int(item.found_on_web),
                    int(item.catch_all_domain is True),
                    item.verification_method.value,
                    item.verification_verdict.value,
                    item.confidence,
                    item.notes,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


@app.command("enrich")
def enrich(
    csv_path: Path,
    force: bool = typer.Option(False, "--force"),
    max_people: Annotated[int | None, typer.Option("--max-people", min=1)] = None,
    replay: Annotated[Path | None, typer.Option("--replay", exists=True, file_okay=False)] = None,
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Enrich contacts; replay mode is fully offline and bypasses SMTP/LLM."""
    contacts = _read_contacts(csv_path)
    if max_people is not None:
        contacts = contacts[:max_people]
    if dry_run:
        console.print(f"Would process {len(contacts)} contacts; no requests made")
        return
    active_settings = _replay_settings(settings) if replay is not None else settings
    if replay is None:
        _require_smtp_mail_from()
        _require_operator_inputs()
    from leadforge.pipeline import run_enrichment

    if replay is not None:
        replay_fetcher = ReplayFetcher(replay)
        fetch_operation: WalkFetchFn = replay_fetcher

        async def offline_llm(**kwargs):
            raise LLMNotConfigured("Replay mode uses deterministic extraction")

        async def offline_verify(candidates):
            return candidates

        llm_fn, verify_fn = offline_llm, offline_verify
    else:
        fetcher = Fetcher(settings)

        async def live_fetch(url: str):
            return await fetcher.fetch(url)

        async def live_llm(**kwargs):
            from leadforge.llm.prompts import build_user_message
            from leadforge.llm.schemas import PageExtraction

            return await chat_json(
                SYSTEM_PROMPT,
                build_user_message(
                    person_name=kwargs["contact"].full_name,
                    company=kwargs["contact"].company or kwargs["contact"].domain,
                    role=kwargs["contact"].title or "",
                    page_text=kwargs["page_text"],
                    candidate_links=kwargs["candidate_links"],
                ),
                PageExtraction,
                settings=settings,
                prompt_version=PROMPT_VERSION,
            )

        async def live_verify(candidates):
            if not candidates:
                return candidates
            return await verify_candidates(candidates[0].contact, candidates, settings)

        fetch_operation, llm_fn, verify_fn = live_fetch, live_llm, live_verify

    results = asyncio.run(
        run_enrichment(
            contacts,
            settings=active_settings,
            fetch_fn=fetch_operation,
            llm_fn=llm_fn,
            verify_fn=verify_fn,
            persist_fn=functools.partial(_persist_walk_result, active_settings=active_settings),
            force=force,
        )
    )
    _write_enrichment_outputs(results, active_settings)
    console.print(f"Processed {len(results)} contacts; outputs written to {settings.output_dir}")


@app.command("run-daily")
def run_daily(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Run the capped discover, people, enrich, and report stages with a resumable lock."""
    companies_path = settings.input_dir / "companies.csv"
    profile_path = settings.data_dir / "profile.json"
    if not companies_path.is_file() or not profile_path.is_file():
        console.print("run-daily requires data/input/companies.csv and data/profile.json")
        raise typer.Exit(code=2)
    companies = read_companies(companies_path)
    if dry_run:
        console.print("run-daily plan (no network requests):")
        for instruction in plan_discovery(companies, settings):
            console.print(f"  {instruction}")
        console.print("  people: up to 3 per company within host budgets")
        console.print(
            f"  enrich: at most {min(20, _outreach_cap())} people with configured walk budgets"
        )
        console.print("  report")
        return

    lock_path = settings.cache_dir / "run-daily.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        console.print("run-daily is already running (lock file exists)")
        raise typer.Exit(code=2) from None
    os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
    os.close(descriptor)
    state_path = settings.cache_dir / "run-daily-state.json"
    today_key = datetime.now(UTC).date().isoformat()
    try:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("date") != today_key:
            state = {"date": today_key, "completed": []}
        completed = set(state.get("completed", []))

        def finish_stage(name: str) -> None:
            completed.add(name)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"date": today_key, "completed": sorted(completed)}),
                encoding="utf-8",
            )
            temporary.replace(state_path)

        try:
            if "discover" not in completed:
                discover(companies_path, dry_run=False, include_hn=True, limit=None)
                finish_stage("discover")
            if "people" not in completed:
                for company_row in companies:
                    people(
                        company_row.name,
                        company_row.domain,
                        max_people=3,
                        replay=None,
                        dry_run=False,
                    )
                finish_stage("people")
            if "enrich" not in completed:
                contacts_path = _daily_contacts_csv(settings, companies)
                if contacts_path is not None:
                    enrich(
                        contacts_path,
                        force=False,
                        max_people=min(20, _outreach_cap()),
                        replay=None,
                        dry_run=False,
                    )
                finish_stage("enrich")
            if "report" not in completed:
                report(since=30)
                finish_stage("report")
        except Exception as exc:
            from leadforge.web.errors import FetchError

            if isinstance(exc, FetchError):
                console.print(f"run-daily stopped on policy/fetch failure: {type(exc).__name__}")
            else:
                console.print(
                    f"run-daily stage failed: {type(exc).__name__}; completed stages are saved"
                )
            raise typer.Exit(code=1) from None
    finally:
        lock_path.unlink(missing_ok=True)


def _outreach_cap() -> int:
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        return daily_cap(conn)
    finally:
        conn.close()


def _daily_contacts_csv(active_settings, companies) -> Path | None:
    domains = [item.domain.lower() for item in companies]
    if not domains:
        return None
    placeholders = ",".join("?" for _ in domains)
    conn = connect(active_settings.db_path)
    try:
        init_schema(conn)
        rows = conn.execute(
            f"SELECT p.first_name,p.last_name,p.title,c.name,c.domain FROM person p "
            f"JOIN company c ON c.id=p.company_id WHERE lower(c.domain) IN ({placeholders}) "
            "AND p.first_name IS NOT NULL AND p.last_name IS NOT NULL ORDER BY p.id",
            domains,
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    path = active_settings.cache_dir / "run-daily-contacts.csv"
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=["first_name", "last_name", "domain", "company", "title"]
        )
        writer.writeheader()
        writer.writerows(
            {
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "domain": row["domain"],
                "company": row["name"],
                "title": row["title"] or "",
            }
            for row in rows
        )
    return path


def _read_contacts(csv_path: Path) -> list[Contact]:
    contacts = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            contacts.append(
                Contact(
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    domain=row["domain"],
                    company=row.get("company") or None,
                    title=row.get("title") or None,
                    source="csv",
                )
            )
    return contacts


def _require_operator_inputs() -> None:
    if (
        not (settings.input_dir / "companies.csv").is_file()
        or not (settings.data_dir / "profile.json").is_file()
    ):
        console.print(
            "[red]Live discovery refused:[/] provide data/input/companies.csv and "
            "ingest a resume first."
        )
        raise typer.Exit(code=2)


def _write_enrichment_outputs(results, active_settings) -> None:
    draft_checks = _write_drafts(results, active_settings)
    active_settings.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    enriched_path = active_settings.output_dir / f"enriched_{timestamp}.csv"
    fields = [
        "first_name",
        "last_name",
        "company",
        "domain",
        "best_email",
        "evidence_tier",
        "confidence",
        "verdict",
        "catch_all",
        "sources",
        "stop_reason",
        "fetches_used",
        "provenance",
    ]
    with enriched_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for result in results:
            best = result.best_email
            writer.writerow(
                {
                    "first_name": result.contact.first_name,
                    "last_name": result.contact.last_name,
                    "company": result.contact.company or "",
                    "domain": result.contact.domain,
                    "best_email": best.email if best else "",
                    "evidence_tier": best.evidence_tier if best else "",
                    "confidence": f"{best.confidence:.2f}" if best else "",
                    "verdict": best.verification_verdict.value if best else "unknown",
                    "catch_all": best.catch_all_domain if best else "",
                    "sources": ";".join(sorted({c.source for c in result.all_candidates})),
                    "stop_reason": result.stop_reason or "",
                    "fetches_used": result.fetches_used,
                    "provenance": ";".join(result.provenance),
                }
            )
    review_path = active_settings.output_dir / "review.csv"
    review_fields = [
        "email",
        "company",
        "first_name",
        "last_name",
        "evidence_tier",
        "confidence",
        "provenance",
        "approved",
        "draft_quality",
        "draft_issues",
    ]
    with review_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=review_fields)
        writer.writeheader()
        for result in results:
            for candidate in result.all_candidates:
                if is_sendable(candidate, active_settings) and not _candidate_blocked(
                    candidate, result.contact, active_settings
                ):
                    issues = draft_checks.get(candidate.email, ["NEEDS_MANUAL_PERSONALIZATION"])
                    writer.writerow(
                        {
                            "email": candidate.email,
                            "company": result.contact.company or result.contact.domain,
                            "first_name": result.contact.first_name,
                            "last_name": result.contact.last_name,
                            "evidence_tier": candidate.evidence_tier,
                            "confidence": f"{candidate.confidence:.2f}",
                            "provenance": ";".join(result.provenance),
                            "approved": "",
                            "draft_quality": "pass" if not issues else "fail",
                            "draft_issues": "; ".join(issues),
                        }
                    )
    _write_drafts(results, active_settings)
    console.print(f"Enriched CSV: {enriched_path}")
    console.print(f"Review CSV: {review_path}")


def _write_drafts(results, active_settings) -> dict[str, list[str]]:
    drafts_dir = active_settings.output_dir / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    checks: dict[str, list[str]] = {}
    for result in results:
        for candidate in result.all_candidates:
            if not is_sendable(candidate, active_settings) or _candidate_blocked(
                candidate, result.contact, active_settings
            ):
                continue
            safe_company = "".join(
                ch.lower() if ch.isalnum() else "_"
                for ch in (result.contact.company or result.contact.domain)
            )
            safe_last = "".join(
                ch.lower() if ch.isalnum() else "_" for ch in result.contact.last_name
            )
            path = drafts_dir / f"{safe_company}__{safe_last}.md"
            evidence = _draft_evidence(result.contact, active_settings, result.provenance)
            draft = _compose_draft(result.contact, candidate.email, evidence)
            path.write_text(draft, encoding="utf-8")
            checks[candidate.email] = quality_issues(draft)
    return checks


def _candidate_blocked(candidate: EmailCandidate, contact: Contact, active_settings) -> bool:
    conn = connect(active_settings.db_path)
    try:
        init_schema(conn)
        return is_suppressed_or_contacted(
            conn,
            candidate.email,
            first_name=contact.first_name,
            last_name=contact.last_name,
            domain=contact.domain,
        )
    finally:
        conn.close()


def _draft_evidence(contact: Contact, active_settings, provenance: list[str]):
    conn = connect(active_settings.db_path)
    try:
        init_schema(conn)
        row = conn.execute(
            "SELECT e.kind,e.value,pg.url FROM evidence e "
            "JOIN person p ON p.id=e.person_id JOIN company c ON c.id=p.company_id "
            "LEFT JOIN page pg ON pg.id=e.page_id WHERE e.professional=1 "
            "AND lower(p.first_name)=lower(?) AND lower(p.last_name)=lower(?) "
            "AND lower(c.domain)=lower(?) ORDER BY e.created_at DESC LIMIT 1",
            (contact.first_name, contact.last_name, contact.domain),
        ).fetchone()
        if row is None:
            return None
        url = row["url"]
        if not url and len(provenance) == 1:
            url = provenance[0]
        return (str(row["kind"]), str(row["value"]), str(url)) if url else None
    finally:
        conn.close()


def _compose_draft(contact: Contact, email: str, evidence) -> str:
    if not evidence:
        return "NEEDS_MANUAL_PERSONALIZATION\n"
    kind, value, url = evidence
    if not url.startswith(("https://", "http://")):
        return "NEEDS_MANUAL_PERSONALIZATION\n"
    company = contact.company or contact.domain
    detail = " ".join(value.split())[:220]
    return (
        f"Subject: Quick question about work at {company}\n\n"
        f"Hi {contact.first_name},\n\n"
        f'I found your professional {kind.replace("_", " ")} about "{detail}" '
        f"([source]({url})). That concrete detail helped me understand part of the work "
        f"happening at {company}.\n\n"
        "I'm exploring opportunities where I could contribute my experience and am trying "
        "to learn how early-career candidates should approach teams like yours. I'll keep "
        "this brief, and I can share a concise background summary if useful.\n\n"
        "Would you be open to a brief conversation about how early-career candidates can "
        "best approach your team?\n\n"
        'If you prefer no further messages, reply "opt out" and I won\'t follow up.\n\n'
        "Best,\nYour name\n"
    )


@app.command("drafts")
def drafts(
    dry_run: bool = typer.Option(False, "--dry-run"),
    check_only: bool = typer.Option(False, "--check"),
) -> None:
    """Write conservative draft placeholders for sendable review rows; never sends."""
    review_path = settings.output_dir / "review.csv"
    if not review_path.is_file():
        console.print("No review.csv exists yet")
        return
    with review_path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    sendable = [row for row in rows if row.get("email")]
    if dry_run:
        console.print(f"Would write {len(sendable)} draft placeholders; no files written")
        return
    # Review rows contain no professional project evidence, so never invent personalization.
    target = settings.output_dir / "drafts"
    target.mkdir(parents=True, exist_ok=True)
    if not check_only:
        for row in sendable:
            company = "".join(ch.lower() if ch.isalnum() else "_" for ch in row["company"])
            last = "".join(ch.lower() if ch.isalnum() else "_" for ch in row["last_name"])
            path = target / f"{company}__{last}.md"
            if not path.exists():
                path.write_text("NEEDS_MANUAL_PERSONALIZATION\n", encoding="utf-8")
    issues = _refresh_review_quality(review_path, target)
    verb = "Checked" if check_only else "Wrote or checked"
    console.print(f"{verb} {len(sendable)} drafts; {issues} need manual personalization or repair")


def _refresh_review_quality(review_path: Path, drafts_dir: Path) -> int:
    if not review_path.is_file():
        return 0
    with review_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    for field in ("draft_quality", "draft_issues"):
        if field not in fields:
            fields.append(field)
    failing = 0
    conn = connect(settings.db_path)
    init_schema(conn)
    retained = []
    for row in rows:
        company = "".join(ch.lower() if ch.isalnum() else "_" for ch in row["company"])
        last = "".join(ch.lower() if ch.isalnum() else "_" for ch in row["last_name"])
        if is_suppressed_or_contacted(
            conn,
            row["email"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            company=row["company"],
        ):
            continue
        draft_path = drafts_dir / f"{company}__{last}.md"
        text = (
            draft_path.read_text(encoding="utf-8")
            if draft_path.exists()
            else "NEEDS_MANUAL_PERSONALIZATION\n"
        )
        issues = quality_issues(text)
        row["draft_quality"] = "pass" if not issues else "fail"
        row["draft_issues"] = "; ".join(issues)
        failing += bool(issues)
        retained.append(row)
    conn.close()
    rows = retained
    with review_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return failing


def _refresh_existing_review() -> None:
    _refresh_review_quality(settings.output_dir / "review.csv", settings.output_dir / "drafts")


@app.command("report")
def report(since: Annotated[int, typer.Option("--since", min=1)] = 30) -> None:
    """Summarize recent pipeline, source, SMTP, budget, and LLM activity."""
    cutoff = (datetime.now(UTC) - timedelta(days=since)).isoformat()
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        attempted = conn.execute(
            "SELECT COUNT(*) AS n FROM person WHERE updated_at>=?", (cutoff,)
        ).fetchone()["n"]
        statuses = conn.execute(
            "SELECT status,COUNT(*) AS n FROM person WHERE updated_at>=? GROUP BY status",
            (cutoff,),
        ).fetchall()
        tiers = conn.execute(
            "SELECT ec.evidence_tier,COUNT(DISTINCT ec.person_id) AS n "
            "FROM email_candidate ec JOIN person p ON p.id=ec.person_id "
            "WHERE p.updated_at>=? GROUP BY ec.evidence_tier",
            (cutoff,),
        ).fetchall()
        sources = conn.execute(
            "SELECT ec.source,COUNT(DISTINCT ec.person_id) AS n FROM email_candidate ec "
            "JOIN person p ON p.id=ec.person_id WHERE p.updated_at>=? GROUP BY ec.source",
            (cutoff,),
        ).fetchall()
        verdicts = conn.execute(
            "SELECT verification_verdict,COUNT(*) AS n FROM email_candidate ec "
            "JOIN person p ON p.id=ec.person_id WHERE p.updated_at>=? "
            "GROUP BY verification_verdict",
            (cutoff,),
        ).fetchall()
        paused = conn.execute(
            "SELECT host,paused_until,pause_reason FROM domain_state "
            "WHERE paused_until IS NOT NULL AND paused_until>? ORDER BY host",
            (datetime.now(UTC).isoformat(),),
        ).fetchall()
        day = datetime.now(UTC).date().isoformat()
        usage = conn.execute("SELECT count FROM llm_usage WHERE day=?", (day,)).fetchone()
        fetch_logs = conn.execute(
            "SELECT message FROM run_log WHERE stage='enrich' AND ts>=?", (cutoff,)
        ).fetchall()
        outreach_rates = conn.execute(
            "SELECT ec.evidence_tier,ec.source,COUNT(*) AS sent,"
            "SUM(o.outcome='reply') AS replies,SUM(o.outcome='bounce') AS bounces "
            "FROM outreach o JOIN email_candidate ec ON ec.person_id=o.person_id "
            "AND lower(ec.email)=lower(o.email) WHERE o.sent_at>=? "
            "GROUP BY ec.evidence_tier,ec.source ORDER BY ec.evidence_tier,ec.source",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    status_counts = {row["status"]: int(row["n"]) for row in statuses}
    tier_counts = {row["evidence_tier"]: int(row["n"]) for row in tiers}
    found_fetches = []
    for row in fetch_logs:
        message = str(row["message"])
        if "status=found" in message:
            import re

            match = re.search(r"fetches=(\d+)", message)
            if match:
                found_fetches.append(int(match.group(1)))
    avg_fetches = sum(found_fetches) / len(found_fetches) if found_fetches else 0.0
    console.print(f"People attempted: {attempted}")
    console.print(
        "Found by evidence tier: "
        + ", ".join(f"{tier}={tier_counts.get(tier, 0)}" for tier in ("A", "B", "C"))
    )
    console.print(f"Not found: {status_counts.get('not_found', 0)}")
    console.print(f"Average fetches per found: {avg_fetches:.2f}")
    console.print("Per-source yield:")
    if sources:
        for row in sources:
            console.print(f"  {row['source']}: {row['n']}")
    else:
        console.print("  none")
    console.print("SMTP verdict distribution:")
    if verdicts:
        for row in verdicts:
            console.print(f"  {row['verification_verdict']}: {row['n']}")
    else:
        console.print("  none")
    console.print("Blocked/paused domains:")
    if paused:
        for row in paused:
            console.print(f"  {row['host']}: {row['pause_reason']} until {row['paused_until']}")
    else:
        console.print("  none")
    console.print(f"LLM calls used today: {int(usage['count']) if usage else 0}")
    console.print("Outreach reply/bounce rates by evidence tier and source:")
    if outreach_rates:
        for row in outreach_rates:
            sent = int(row["sent"])
            replies = int(row["replies"] or 0)
            bounces = int(row["bounces"] or 0)
            bounce_rate = bounces / sent if sent else 0.0
            console.print(
                f"  tier={row['evidence_tier']} source={row['source']}: sent={sent}, "
                f"replies={replies} ({replies / sent:.1%}), bounces={bounces} "
                f"({bounce_rate:.1%})"
            )
            if bounce_rate >= 0.03:
                console.print("  [bold red]Bounce alert: rate is at or above 3%[/]")
    else:
        console.print("  none")


@domains_app.command("unpause")
def unpause_domain(host: str) -> None:
    """Clear a host pause after the operator has reviewed the cause."""
    conn = connect(settings.db_path)
    try:
        cursor = conn.execute(
            "UPDATE domain_state SET paused_until=NULL,pause_reason=NULL WHERE host=?",
            (host.lower().rstrip("."),),
        )
        conn.commit()
    finally:
        conn.close()
    console.print(f"Unpaused {host}" if cursor.rowcount else f"No pause recorded for {host}")


@app.command("purge")
def purge(
    older_than: Annotated[int, typer.Option("--older-than", min=1)] = 30,
    purge_all: bool = typer.Option(False, "--all"),
) -> None:
    """Remove expired local evidence/cache data."""
    cutoff = (datetime.now(UTC) - timedelta(days=older_than)).isoformat()
    if purge_all and typer.prompt("Type ERASE ALL LEADFORGE RECORDS to continue") != (
        "ERASE ALL LEADFORGE RECORDS"
    ):
        console.print("Purge cancelled")
        raise typer.Exit(code=1)
    conn = connect(settings.db_path)
    counts: dict[str, int] = {}
    try:
        init_schema(conn)
        if purge_all:
            for table in ("email_candidate", "evidence", "person", "job", "page", "company"):
                counts[table] = conn.execute(f"DELETE FROM {table}").rowcount
        else:
            counts["email_candidate"] = conn.execute(
                "DELETE FROM email_candidate WHERE person_id IN "
                "(SELECT id FROM person WHERE created_at<?)",
                (cutoff,),
            ).rowcount
            counts["evidence"] = conn.execute(
                "DELETE FROM evidence WHERE created_at<? OR person_id IN "
                "(SELECT id FROM person WHERE created_at<?)",
                (cutoff, cutoff),
            ).rowcount
            counts["person"] = conn.execute(
                "DELETE FROM person WHERE created_at<?", (cutoff,)
            ).rowcount
            counts["job"] = conn.execute("DELETE FROM job WHERE last_seen<?", (cutoff,)).rowcount
            counts["page"] = conn.execute(
                "DELETE FROM page WHERE expires_at<?", (datetime.now(UTC).isoformat(),)
            ).rowcount
            counts["company"] = conn.execute(
                "DELETE FROM company WHERE created_at<? AND NOT EXISTS "
                "(SELECT 1 FROM person WHERE person.company_id=company.id)",
                (cutoff,),
            ).rowcount
        conn.commit()
    finally:
        conn.close()
    cache_pages = settings.cache_dir / "pages"
    removed_files = 0
    if cache_pages.is_dir():
        cutoff_timestamp = datetime.now(UTC).timestamp() - older_than * 86400
        for path in cache_pages.rglob("*"):
            if path.is_file() and (purge_all or path.stat().st_mtime < cutoff_timestamp):
                path.unlink()
                removed_files += 1
    counts["cache_files"] = removed_files
    for table, count in counts.items():
        console.print(f"{table}: {count}")


def _established_pattern(domain: str) -> str | None:
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


async def _verify_csv(csv_path: Path) -> None:
    candidates: list[EmailCandidate] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            contact = Contact(
                first_name=row["first_name"],
                last_name=row["last_name"],
                domain=row["domain"],
            )
            candidates.append(
                EmailCandidate(email=row["email"], contact=contact, source="csv", confidence=0.5)
            )
    results = (
        await verify_candidates(candidates[0].contact, candidates, settings) if candidates else []
    )
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = settings.output_dir / f"verified_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv"
    fields = list(results[0].model_dump().keys()) if results else ["email"]
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for result in results:
            values = result.model_dump(mode="json")
            writer.writerow({field: values.get(field) for field in fields})
    counts: dict[str, int] = {}
    for result in results:
        verdict = result.verification_verdict.value
        counts[verdict] = counts.get(verdict, 0) + 1
    console.print(f"Verified {len(results)} candidates → {output_path}")
    for verdict, count in sorted(counts.items()):
        console.print(f"  {verdict}: {count}")
    console.print(f"  catch-all: {sum(result.catch_all_domain is True for result in results)}")
    console.print(
        f"  unreachable: {sum('unreachable' in (result.notes or '') for result in results)}"
    )


@app.command("init-db")
def init_db() -> None:
    """Create the local SQLite database and initialize its schema."""
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        console.print(
            f"Database ready: {settings.db_path.resolve()} (schema v{schema_version(conn)})"
        )
    finally:
        conn.close()


def _backup_database() -> Path:
    source_path = Path(settings.db_path)
    source = connect(source_path)
    try:
        init_schema(source)
        backup_dir = settings.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target_path = backup_dir / f"leadforge_{stamp}.db"
        target = sqlite3.connect(str(target_path))
        try:
            source.backup(target)
        finally:
            target.close()
        return target_path
    finally:
        source.close()


@app.command("backup")
def backup() -> None:
    """Create a consistent SQLite backup in the ignored data/backups directory."""
    console.print(f"Database backup: {_backup_database()}")


@app.command("reset-db")
def reset_db(confirm: bool = typer.Option(False, "--confirm")) -> None:
    """Back up the database, then recreate an empty schema after typed confirmation."""
    if not confirm:
        console.print("Database reset requires --confirm and typed confirmation.")
        raise typer.Exit(code=2)
    if typer.prompt("Type RESET LEADFORGE DATABASE") != "RESET LEADFORGE DATABASE":
        console.print("Database reset cancelled")
        raise typer.Exit(code=1)
    backup_path = _backup_database()
    database_path = Path(settings.db_path)
    for path in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        path.unlink(missing_ok=True)
    conn = connect(database_path)
    try:
        init_schema(conn)
    finally:
        conn.close()
    console.print(f"Database reset; backup saved to {backup_path}")


@app.command("mark-sent")
def mark_sent(
    person_id: int,
    email: str,
    channel: str = typer.Option("email", "--channel"),
    follow_up_due: str | None = typer.Option(None, "--follow-up-due"),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Record that the operator manually sent an outreach message."""
    normalized = email.strip().casefold()
    if "@" not in normalized:
        console.print("A valid email address is required")
        raise typer.Exit(code=2)
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        person = conn.execute("SELECT id FROM person WHERE id=?", (person_id,)).fetchone()
        candidate = conn.execute(
            "SELECT evidence_tier,verification_verdict,confidence FROM email_candidate "
            "WHERE person_id=? AND lower(email)=?",
            (person_id, normalized),
        ).fetchone()
        if person is None or candidate is None:
            console.print("Person and matching email candidate must exist")
            raise typer.Exit(code=2)
        if candidate["evidence_tier"] != "A":
            console.print("Only Tier A addresses may be marked sent")
            raise typer.Exit(code=2)
        if (
            float(candidate["confidence"]) < settings.min_confidence_to_emit
            or candidate["verification_verdict"] == "kill"
        ):
            console.print("Only eligible, non-killed candidates may be marked sent")
            raise typer.Exit(code=2)
        if is_suppressed_or_contacted(conn, normalized, person_id=person_id):
            console.print("Address or person is suppressed or already contacted")
            raise typer.Exit(code=2)
        cap = daily_cap(conn)
        used = sent_today(conn)
        if used >= cap:
            console.print(f"Daily outreach ramp cap reached ({used}/{cap})")
            raise typer.Exit(code=2)
        conn.execute(
            "INSERT INTO outreach(person_id,email,sent_at,channel,outcome,follow_up_due,notes) "
            "VALUES(?,?,?,?, 'none',?,?)",
            (person_id, normalized, datetime.now(UTC).isoformat(), channel, follow_up_due, notes),
        )
        conn.commit()
    finally:
        conn.close()
    _refresh_existing_review()
    console.print("Manual outreach recorded")


@app.command("mark-outcome")
def mark_outcome(person_id: int, email: str, outcome: str) -> None:
    """Record the response or opt-out outcome for a manually sent message."""
    from leadforge.outreach import OUTCOMES

    if outcome not in OUTCOMES:
        console.print(f"Outcome must be one of: {', '.join(OUTCOMES)}")
        raise typer.Exit(code=2)
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        cursor = conn.execute(
            "UPDATE outreach SET outcome=? WHERE person_id=? AND lower(email)=?",
            (outcome, person_id, email.strip().casefold()),
        )
        conn.commit()
    finally:
        conn.close()
    if cursor.rowcount == 0:
        console.print("No matching outreach record")
        raise typer.Exit(code=2)
    if outcome == "opt_out":
        suppress(email)
    else:
        _refresh_existing_review()
        console.print("Outreach outcome recorded")


@app.command("suppress")
def suppress(value: str) -> None:
    """Suppress an exact email address or an entire domain from review and today."""
    normalized = value.strip().casefold().rstrip(".")
    kind = "email" if "@" in normalized else "domain"
    if not normalized or (kind == "email" and normalized.count("@") != 1):
        console.print("Provide one email address or a domain")
        raise typer.Exit(code=2)
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO suppression(kind,value,created_at) VALUES(?,?,?)",
            (kind, normalized, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    _refresh_existing_review()
    console.print(f"Suppressed {kind}")


@app.command("today")
def today() -> None:
    """List eligible Tier A people under the current outreach ramp cap."""
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        cap = daily_cap(conn)
        used = sent_today(conn)
        remaining = max(0, cap - used)
        rows = conn.execute(
            "SELECT p.id,p.first_name,p.last_name,p.title,c.name AS company,ec.email,ec.confidence "
            "FROM email_candidate ec JOIN person p ON p.id=ec.person_id "
            "JOIN company c ON c.id=p.company_id WHERE ec.evidence_tier='A' "
            "AND ec.confidence>=? AND COALESCE(ec.verification_verdict,'unknown')!='kill' "
            "ORDER BY ec.confidence DESC,p.id",
            (settings.min_confidence_to_emit,),
        ).fetchall()
        eligible = [
            row
            for row in rows
            if not is_suppressed_or_contacted(conn, str(row["email"]), person_id=int(row["id"]))
        ][:remaining]
    finally:
        conn.close()
    console.print(f"Today's list: {len(eligible)} of {remaining} available; ramp cap {used}/{cap}")
    for row in eligible:
        console.print(
            f"  person_id={row['id']} {row['first_name']} {row['last_name']} — "
            f"{row['title'] or 'role unknown'} at {row['company']} ({row['email']})"
        )


if __name__ == "__main__":
    app()
