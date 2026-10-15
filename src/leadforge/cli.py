import asyncio
import csv
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import dns.resolver
import typer
from rich.console import Console

from leadforge import __version__
from leadforge.config import settings
from leadforge.db import connect, init_schema, schema_version
from leadforge.llm import LLMNotConfigured, LLMOutputError, chat_json
from leadforge.llm.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from leadforge.llm.schemas import PingResponse
from leadforge.models import Contact, EmailCandidate
from leadforge.stages.pattern_learn import is_established
from leadforge.stages.replay import ReplayFetcher
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
console = Console()


@app.command()
def version() -> None:
    """Print the current version."""
    console.print(f"leadforge [bold cyan]{__version__}[/]")


@app.command()
def doctor() -> None:
    """Check Python, dependencies, and external tool availability."""
    console.print(f"[bold]Python:[/] {sys.version.split()[0]}")
    console.print(f"[bold]Data dir:[/] {settings.data_dir.resolve()}")
    console.print(f"[bold]SMTP MAIL FROM:[/] {settings.smtp_mail_from or '(empty)'}")
    mail_from_problem = smtp_mail_from_problem(settings.smtp_mail_from)
    if mail_from_problem:
        console.print(f"[bold yellow]Warning:[/] {mail_from_problem}")
    dns_nameservers = settings.dns_nameservers or dns.resolver.get_default_resolver().nameservers
    dns_source = "configured" if settings.dns_nameservers else "system"
    console.print(f"[bold]DNS resolver:[/] {', '.join(map(str, dns_nameservers))} ({dns_source})")
    console.print(f"[bold]Min confidence:[/] {settings.min_confidence_to_emit}")
    console.print(f"[bold]LLM API key:[/] {'set' if settings.llm_api_key else 'not set'}")
    policy = DomainPolicy.load(settings.domain_policy_path, settings)
    console.print(f"[bold]Tier 1 backend:[/] {tier1_backend()}")
    obscura = settings.obscura_path
    if obscura and obscura.is_file():
        console.print(f"[bold]Obscura:[/] {_obscura_version(obscura)} ({obscura})")
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
        console.print(f"[bold]Port 25:[/] check failed ({exc})")
    else:
        console.print(f"[bold]Port 25:[/] {port25_status.value}")
        if port25_status is Port25Status.DNS_FAILED:
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

    console.print("[bold]External tools:[/]")
    for tool, hint in (
        ("es", "email-sleuth (cargo install email-sleuth)"),
        ("websearch", "websearch-skill (pip install websearch-skill)"),
    ):
        path = shutil.which(tool)
        if path:
            console.print(f"  [green]+[/] {tool}: {path}")
        else:
            console.print(f"  [yellow]-[/] {tool}: not found -- {hint}")


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
) -> None:
    """Fetch one URL through the policy, robots, budget, and cache router."""
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


def _terminal_safe_text(value: str, encoding: str | None = None) -> str:
    """Replace characters the active Windows terminal encoding cannot display."""
    terminal_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(terminal_encoding, errors="replace").decode(
        terminal_encoding, errors="replace"
    )


@app.command("llm-ping")
def llm_ping() -> None:
    """Check an explicitly configured OpenAI-compatible LLM endpoint."""
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
def verify(csv_path: Path) -> None:
    """Verify candidate addresses from a CSV without sending email."""
    _require_smtp_mail_from()
    asyncio.run(_verify_csv(csv_path))


def _require_smtp_mail_from() -> None:
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

        fetch_fn = replay_fetcher
        llm_fn = offline_llm
        verify_fn = offline_verify
        starts = replay_fetcher.seeds or seed_urls(company_domain)
    else:
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

        async def fetch_fn(url: str):
            return await fetcher.fetch(url)

        llm_fn = live_llm

        async def verify_fn(candidates: list[EmailCandidate]):
            return await verify_candidates(contact, candidates, settings)

        starts = seed_urls(company_domain)
    try:
        result = asyncio.run(
            walk_person(
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
                seed_urls_override=starts,
                denylist_hosts=load_denylist(settings.denylist_hosts_path),
                persist_fn=_persist_walk_result,
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


def _persist_walk_result(result: WalkResult) -> None:
    """Upsert the person state and persist candidate/evidence provenance locally."""
    now = datetime.now(UTC).isoformat()
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        candidate = result.candidates[0] if result.candidates else None
        contact = candidate.contact if candidate else None
        if contact is None:
            return
        company = conn.execute(
            "SELECT id FROM company WHERE domain=?", (contact.domain.lower(),)
        ).fetchone()
        if company:
            company_id = int(company["id"])
        else:
            company_id = int(
                conn.execute(
                    "INSERT INTO company(name,domain,source,created_at) VALUES(?,?,?,?)",
                    (contact.company or contact.domain, contact.domain.lower(), "walk", now),
                ).lastrowid
            )
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
            person_id = int(
                conn.execute(
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
                ).lastrowid
            )
        result.person_id = person_id
        for kind, value, professional in result.evidence:
            conn.execute(
                """INSERT INTO evidence(person_id,company_id,kind,value,professional,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (person_id, company_id, kind, value, int(professional), now),
            )
        for item in result.candidates:
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


if __name__ == "__main__":
    app()
