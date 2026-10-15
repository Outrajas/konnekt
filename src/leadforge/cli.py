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
    try:
        port25_reachable = asyncio.run(check_port25_reachable(timeout_seconds=5.0))
    except Exception as exc:
        console.print(f"[bold]Port 25:[/] check failed ({exc})")
    else:
        state = "reachable" if port25_reachable else "blocked"
        console.print(f"[bold]Port 25:[/] {state}")
        if not port25_reachable:
            console.print(
                "  SMTP verification will use pattern-only results until outbound port 25 "
                "is reachable."
            )
    console.print()

    console.print("[bold]External tools:[/]")
    for tool, hint in (
        ("es", "email-sleuth (cargo install email-sleuth)"),
        ("forge", "FORGE (pip install forge-enrichment)"),
        ("websearch", "websearch-skill (pip install websearch-skill)"),
    ):
        path = shutil.which(tool)
        if path:
            console.print(f"  [green]+[/] {tool}: {path}")
        else:
            console.print(f"  [yellow]-[/] {tool}: not found -- {hint}")


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
