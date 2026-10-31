"""Which hostnames are real, and which are placeholders invented by a board parser.

Board adapters that do not know a company's real domain synthesize one so the
row stays joinable — ``acme.wellfound``, ``acme.remoteok``, ``acme.naukri``.
Those names are never resolvable, so sending them into people search or email
pattern generation produces confident nonsense (a live run generated twelve
"candidates" at ``ao-garcia-agency.remoteok`` before this guard existed).

Keep the placeholder suffixes in one place; the enrichment stages filter on it
and ``data/domain_overrides.json`` can map a placeholder to a real domain.
"""

from __future__ import annotations

import json
from pathlib import Path

# Suffixes a board parser appends when it has a company name but no domain.
SYNTHETIC_TLDS: tuple[str, ...] = (
    "wellfound",
    "remoteok",
    "naukri",
    "instahyre",
    "unstop",
    "weworkremotely",
    "remotive",
    "himalayas",
    "hirist",
    "cutshort",
    "foundit",
    "wtj",
    "yc",
    "hacker-news",
    "test",
    "example",
    "invalid",
)


def is_synthetic_domain(domain: str | None) -> bool:
    """True when the domain is a placeholder no DNS query will ever resolve."""
    if not domain:
        return True
    lowered = domain.strip().lower().rstrip(".")
    return any(lowered.endswith("." + tld) for tld in SYNTHETIC_TLDS)


def sql_synthetic_filter(column: str) -> str:
    """SQL fragment that keeps only rows whose domain looks real."""
    clauses = "".join(f"AND {column} NOT LIKE '%.{tld}' " for tld in SYNTHETIC_TLDS)
    return f"AND {column} IS NOT NULL AND {column} != '' " + clauses


def load_domain_overrides(path: Path) -> dict[str, str]:
    """Placeholder -> real domain map; missing or broken file means no overrides."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).lower(): str(value).strip().lower()
        for key, value in raw.items()
        if not str(key).startswith("_") and value
    }


def resolve_domain(domain: str | None, overrides: dict[str, str]) -> str:
    """Return the real domain for a job/company row, or "" when there is none."""
    if not domain:
        return ""
    key = domain.strip().lower()
    mapped = overrides.get(key, "")
    if mapped:
        return mapped
    return "" if is_synthetic_domain(key) else key
