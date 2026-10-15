import asyncio
import csv
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

from leadforge import __version__
from leadforge.config import settings
from leadforge.db import connect, init_schema, schema_version
from leadforge.models import Contact, EmailCandidate
from leadforge.tools.smtp_verify import check_port25_reachable, verify_candidates
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
    console.print(f"[bold]SMTP from:[/] {settings.smtp_from_address}")
    console.print(f"[bold]Min confidence:[/] {settings.min_confidence_to_emit}")
    console.print(f"[bold]LLM API key:[/] {'set' if settings.llm_api_key else 'not set'}")
    policy = DomainPolicy.load(settings.domain_policy_path, settings)
    console.print(f"[bold]Tier 1 backend:[/] {tier1_backend()}")
    obscura = settings.obscura_path
    tier2_status = (
        str(obscura) if obscura and obscura.exists() else "disabled (OBSCURA_PATH not set)"
    )
    console.print(f"[bold]Tier 2:[/] {tier2_status}")
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
        port25_reachable = asyncio.run(check_port25_reachable(timeout_seconds=5.0))
    except Exception as exc:
        console.print(f"[bold]Port 25:[/] check failed ({exc})")
    else:
        state = "reachable" if port25_reachable else "blocked"
        console.print(f"[bold]Port 25:[/] {state}")
        if not port25_reachable:
            console.print(
                "  SMTP verification will return pattern_only until run from a network "
                "that allows outbound port 25 (e.g. a small VPS)."
            )
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


@app.command()
def fetch(
    url: str,
    tier: int = typer.Option(1, min=1, max=3),
    no_cache: bool = typer.Option(False, "--no-cache"),
    text: bool = typer.Option(False, "--text"),
) -> None:
    """Fetch one URL through the policy, robots, budget, and cache router."""
    try:
        result = asyncio.run(
            Fetcher(settings).fetch(url, min_tier=tier, use_cache=not no_cache)
        )
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
        console.print(cleaned[:500])


@app.command()
def verify(csv_path: Path) -> None:
    """Verify candidate addresses from a CSV without sending email."""
    asyncio.run(_verify_csv(csv_path))


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
                EmailCandidate(
                    email=row["email"], contact=contact, source="csv", confidence=0.5
                )
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
