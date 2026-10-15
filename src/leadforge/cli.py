import shutil
import sys

import typer
from rich.console import Console

from leadforge import __version__
from leadforge.config import settings
from leadforge.db import connect, init_schema, schema_version

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
