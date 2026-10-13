import shutil
import sys

import typer
from rich.console import Console

from leadforge import __version__
from leadforge.config import settings

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


if __name__ == "__main__":
    app()
