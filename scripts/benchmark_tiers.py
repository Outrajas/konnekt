"""Compare policy-routed Tier 1 and optional Tier 2 page fetches."""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console
from rich.table import Table

from leadforge.config import Settings
from leadforge.web.detect import classify_response
from leadforge.web.errors import Blocked, FetchError
from leadforge.web.fetcher import Fetcher
from leadforge.web.policy import DomainPolicy


@dataclass(frozen=True)
class BenchmarkRow:
    domain: str
    tier: str
    classification: str
    latency_ms: int | None
    blocked: str


def read_domains(path: Path) -> list[str]:
    domains = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        host = urlsplit(value if "://" in value else f"https://{value}").hostname
        if not host:
            raise ValueError(f"Invalid company domain: {value}")
        domains.append(host.lower())
    return list(dict.fromkeys(domains))


async def benchmark(
    domains: list[str], *, settings: Settings, dry_run: bool = False, page_path: str = "team"
) -> list[BenchmarkRow]:
    policy = DomainPolicy.load(settings.domain_policy_path, settings)
    fetcher = None if dry_run else Fetcher(settings)
    rows = []
    for domain in domains:
        rule = policy.resolve(domain)
        if dry_run:
            classification = "disabled" if not rule.enabled else "not requested"
            rows.append(BenchmarkRow(domain, "dry-run", classification, None, "not checked"))
            continue
        if not rule.enabled:
            rows.append(BenchmarkRow(domain, "—", "disabled", None, "no"))
            continue
        url = f"https://{domain}/{page_path}"
        max_tier = 2 if settings.obscura_path and settings.obscura_path.is_file() else 1
        started = time.monotonic()
        try:
            assert fetcher is not None
            result = await fetcher.fetch(
                url, min_tier=1, max_tier=max_tier, use_cache=False, purpose="benchmark"
            )
            classification = classify_response(result.status or 0, {}, result.html)
            rows.append(
                BenchmarkRow(
                    domain, str(result.tier), classification,
                    round((time.monotonic() - started) * 1000), "no",
                )
            )
        except Blocked:
            rows.append(
                BenchmarkRow(
                    domain, "1", "blocked", round((time.monotonic() - started) * 1000), "yes"
                )
            )
        except FetchError as exc:
            rows.append(
                BenchmarkRow(
                    domain, "—", type(exc).__name__,
                    round((time.monotonic() - started) * 1000), "no",
                )
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--domains", type=Path, default=Path("data/input/benchmark_domains.txt")
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan without requests")
    parser.add_argument("--path", choices=("team", "about"), default="team")
    args = parser.parse_args()
    if not args.domains.is_file():
        parser.error(f"domain file not found: {args.domains}")
    settings = Settings()
    domains = read_domains(args.domains)
    rows = asyncio.run(
        benchmark(domains, settings=settings, dry_run=args.dry_run, page_path=args.path)
    )
    table = Table("Domain", "Tier", "Classification", "Latency ms", "Blocked")
    for row in rows:
        table.add_row(
            row.domain,
            row.tier,
            row.classification,
            str(row.latency_ms) if row.latency_ms is not None else "—",
            row.blocked,
        )
    Console().print(table)


if __name__ == "__main__":
    main()
