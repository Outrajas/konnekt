"""Validate a companies CSV before a live run: real domain, resolves, not a board placeholder.

The enrichment stages only work on companies that have a registrable domain the
pipeline can actually crawl. Board-sourced rows carry placeholders
(``acme.wellfound``, ``discord.wwr``) which silently produce nothing, so this
script is the gate between "I have a list of names" and "a run that yields".

Usage:
    python scripts/validate_companies.py data/input/companies.csv --dry-run
    python scripts/validate_companies.py data/input/companies.csv \
        --out data/input/companies.verified.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import dns.exception
import dns.resolver

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from leadforge.sources.domains import is_synthetic_domain  # noqa: E402

FIELDS = ("name", "domain", "note")


def resolves(domain: str, *, timeout: float = 5.0) -> bool:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    for record in ("A", "AAAA"):
        try:
            resolver.resolve(domain, record)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.Timeout,
                dns.resolver.NoNameservers):
            continue
        else:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--out", default="")
    parser.add_argument("--dry-run", action="store_true", help="skip DNS checks")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    path = Path(args.csv_path)
    if not path.exists():
        print(f"missing {path} - copy companies.template.csv and fill it in", file=sys.stderr)
        return 2

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("name", "").strip()]

    if not rows:
        print("CSV has no rows with a name", file=sys.stderr)
        return 2
    if "domain" not in rows[0]:
        print("CSV needs name,domain columns", file=sys.stderr)
        return 2

    good, bad = [], []
    for row in rows:
        name = row["name"].strip()
        domain = row["domain"].strip().lower().removeprefix("https://").removeprefix("http://")
        domain = domain.split("/")[0]
        if is_synthetic_domain(domain):
            bad.append((name, domain, "board placeholder or empty"))
            continue
        if not args.dry_run and not resolves(domain, timeout=args.timeout):
            bad.append((name, domain, "does not resolve"))
            continue
        good.append({"name": name, "domain": domain, "note": ""})

    print(f"usable: {len(good)}   rejected: {len(bad)}")
    for name, domain, why in bad[:20]:
        print(f"  - {name:<28} {domain:<28} {why}")

    out = Path(args.out) if args.out else path.with_suffix(".verified.csv")
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(good)
    print(f"\nwrote {len(good)} rows to {out}")
    print("point discover at it:  leadforge discover --companies", out)
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
