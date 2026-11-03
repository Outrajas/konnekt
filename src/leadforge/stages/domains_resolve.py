"""Resolve board placeholders (``discord.wwr``) into real domains (``discord.com``).

Board parsers invent a domain so every job row stays joinable, which means the
enrichment stages correctly refuse to touch those companies. This module walks a
company name through likely domains, proves the homepage belongs to that company,
then rewrites the company row and records the mapping in
``data/domain_overrides.json`` so re-runs are free.

Guessing a domain is cheap; *proving* it is the point. Every candidate must
resolve in DNS and then serve a homepage whose title or body names the company.
Anything unproven is left alone, because a wrong domain turns into a wrong email
and a bounce.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import dns.exception
import dns.resolver

from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.sources.domains import is_synthetic_domain, load_domain_overrides

SUFFIXES = (".com", ".ai", ".in", ".io", ".co", ".org", ".net", ".dev")
STOP_WORDS = {"the", "labs", "lab", "technologies", "technology", "solutions",
              "systems", "inc", "pvt", "ltd", "private", "limited", "analytics",
              "intelligence", "ai", "group", "global", "india"}


def slug_candidates(name: str) -> list[str]:
    """Reasonable hostnames for a company name, most specific first."""
    lowered = name.lower().strip()
    tokens = [t for t in re.split(r"[^a-z0-9]+", lowered) if t]
    if not tokens:
        return []
    compact = "".join(tokens)
    joined = "-".join(tokens)
    trimmed = [t for t in tokens if t not in STOP_WORDS] or tokens
    trimmed_compact = "".join(trimmed)
    bases: list[str] = []
    for base in (compact, trimmed_compact, joined, "-".join(trimmed)):
        if base and base not in bases and len(base) >= 3:
            bases.append(base)
    return [base + suffix for base in bases for suffix in SUFFIXES]


def resolves(domain: str, *, timeout: float = 4.0) -> bool:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    try:
        resolver.resolve(domain, "A")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers,
            dns.exception.Timeout, dns.exception.DNSException):
        return False
    return True


def _name_tokens(name: str) -> list[str]:
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 3]
    return [t for t in tokens if t not in STOP_WORDS] or tokens


def page_mentions(html: str, name: str) -> bool:
    """True when the page plausibly belongs to the company."""
    lowered = html.lower()
    tokens = _name_tokens(name)
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in lowered)
    return hits >= max(1, len(tokens) // 2)


@dataclass
class ResolveResult:
    company: str
    placeholder: str
    domain: str = ""
    reason: str = ""


@dataclass
class ResolveReport:
    results: list[ResolveResult] = field(default_factory=list)
    resolved: int = 0
    attempted: int = 0

    def mapping(self) -> dict[str, str]:
        return {r.placeholder: r.domain for r in self.results if r.domain}


PARKING_MX_HINTS = (
    "above.com",
    "sedoparking",
    "parkingcrew",
    "bodis",
    "dan.com",
    "afternic",
    "hugedomains",
    "domainmarket",
    "parklogic",
)
PARKING_TEXT_HINTS = (
    "domain is for sale",
    "buy this domain",
    "this domain is parked",
    "parked domain",
    "hugedomains",
    "afternic",
)


def is_parked(domain: str) -> bool:
    """True when the domain's MX or homepage says it is a parking page.

    A parked domain answers 250 to every RCPT TO, so treating it as real
    produces twelve confident addresses that all bounce. Granica is the case
    that taught this: granica.com resolves, mentions the name, and is parked on
    above.com.
    """
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 4
    try:
        mx_records = resolver.resolve(domain, "MX")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers,
            dns.exception.DNSException):
        return False
    for record in mx_records:
        exchange = str(record.exchange).lower()
        if any(hint in exchange for hint in PARKING_MX_HINTS):
            return True
    return False


def _looks_parked(html: str) -> bool:
    lowered = html[:6000].lower()
    return any(hint in lowered for hint in PARKING_TEXT_HINTS)


async def resolve_company(name: str, *, fetcher) -> tuple[str, str]:
    """Return (domain, reason); domain is '' when nothing was proven.

    A candidate is only accepted when the homepage names the company *and* the
    domain is not a parking page. Candidates are tried in order and a mismatch
    moves on to the next one instead of giving up on the whole company.
    """
    from leadforge.sources.careers import is_js_shell
    from leadforge.web.errors import FetchError, FetchFailed

    settings = getattr(fetcher, "settings", None)
    browser_ready = bool(getattr(settings, "browser_enabled", False))
    notes: list[str] = []

    for candidate in slug_candidates(name)[:12]:
        if not resolves(candidate):
            continue
        if is_parked(candidate):
            notes.append(f"{candidate} is parked")
            continue
        url = f"https://{candidate}/"
        try:
            page = await fetcher.fetch(url, min_tier=1, max_tier=3, purpose="resolve")
        except FetchError as exc:
            notes.append(f"{candidate}: fetch failed ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001 - a bad candidate is not fatal
            notes.append(f"{candidate}: {type(exc).__name__}")
            continue
        if _looks_parked(page.html):
            notes.append(f"{candidate} serves a parking page")
            continue
        if page_mentions(page.html, name):
            return candidate, "homepage mentions the company"
        if browser_ready and (is_js_shell(page.html) or len(page.html.strip()) < 2000):
            try:
                rendered = await fetcher.fetch(
                    url, min_tier=3, max_tier=3, purpose="resolve", use_cache=False
                )
            except (FetchFailed, Exception):  # noqa: BLE001 - rendering is best-effort
                rendered = None
            if rendered is not None and page_mentions(rendered.html, name):
                return candidate, "rendered homepage mentions the company"
        notes.append(f"{candidate} does not name the company")

    if notes:
        return "", "; ".join(notes[:3])
    return "", "no candidate domain resolved"


async def resolve_synthetic_domains(
    settings: Settings,
    *,
    limit: int = 25,
    dry_run: bool = False,
    fetcher=None,
) -> ResolveReport:
    """Prove real domains for companies that currently hold a placeholder."""
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    report = ResolveReport()

    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        rows = conn.execute("SELECT name, domain FROM company ORDER BY name").fetchall()
    finally:
        conn.close()

    targets = [(r["name"], r["domain"] or "") for r in rows if is_synthetic_domain(r["domain"])]
    report.attempted = min(limit, len(targets))
    for name, placeholder in targets[:limit]:
        domain, reason = await resolve_company(name, fetcher=router)
        report.results.append(ResolveResult(name, placeholder, domain, reason))
        if domain:
            report.resolved += 1

    if not dry_run and report.resolved:
        apply_resolutions(settings, report)
    return report


def apply_resolutions(settings: Settings, report: ResolveReport) -> int:
    """Rewrite company rows and merge the mapping into domain_overrides.json."""
    mapping = report.mapping()
    if not mapping:
        return 0

    overrides_path = settings.data_dir / "domain_overrides.json"
    existing = load_domain_overrides(overrides_path)
    existing.update(mapping)
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    overrides_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    updated = 0
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        for placeholder, domain in mapping.items():
            clash = conn.execute(
                "SELECT id FROM company WHERE domain=? AND domain<>?", (domain, placeholder)
            ).fetchone()
            if clash:
                # Another row already owns the real domain: point jobs at it and
                # drop the duplicate company row.
                conn.execute(
                    "UPDATE job SET company_id=? WHERE company_id=("
                    "SELECT id FROM company WHERE domain=?)",
                    (clash["id"], placeholder),
                )
                conn.execute("DELETE FROM company WHERE domain=?", (placeholder,))
            else:
                conn.execute(
                    "UPDATE company SET domain=?, source='resolved' WHERE domain=?",
                    (domain, placeholder),
                )
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def load_override_map(path: Path) -> dict[str, str]:
    return load_domain_overrides(path)
