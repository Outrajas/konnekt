"""Deterministic work-email pattern generation and learning helpers."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Callable
from pathlib import Path

from leadforge.models import Contact, EmailCandidate

Pattern = Callable[[str, str], str]

_DEFAULT_ORDER = (
    "first.last",
    "flast",
    "firstlast",
    "first",
    "last",
    "f.last",
    "first.l",
    "firstl",
    "last.first",
    "lastf",
    "first_last",
    "first-last",
)
_PATTERNS: dict[str, Pattern] = {
    "first": lambda first, last: first,
    "last": lambda first, last: last,
    "first.last": lambda first, last: f"{first}.{last}",
    "firstlast": lambda first, last: f"{first}{last}",
    "flast": lambda first, last: f"{first[0]}{last}",
    "f.last": lambda first, last: f"{first[0]}.{last}",
    "first.l": lambda first, last: f"{first}.{last[-1]}",
    "firstl": lambda first, last: f"{first}{last[-1]}",
    "last.first": lambda first, last: f"{last}.{first}",
    "lastf": lambda first, last: f"{last}{first[0]}",
    "first_last": lambda first, last: f"{first}_{last}",
    "first-last": lambda first, last: f"{first}-{last}",
}
_HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "sir"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}


def normalize_name_part(value: str, *, drop: set[str] | None = None) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    parts = re.findall(r"[a-z0-9]+", normalized.casefold())
    ignored = drop or set()
    return "".join(part for part in parts if part not in ignored)


def _name_parts(name: str | Contact) -> tuple[str, str]:
    if isinstance(name, Contact):
        first_raw, last_raw = name.first_name, name.last_name
    else:
        words = name.strip().split()
        first_raw, last_raw = (words[0], " ".join(words[1:])) if words else ("", "")
    first = normalize_name_part(first_raw, drop=_HONORIFICS)
    last = normalize_name_part(last_raw, drop=_SUFFIXES)
    if not first or not last:
        raise ValueError("Both first and last names are required to generate email patterns")
    return first, last


def generate(
    contact: Contact, patterns: list[str] | tuple[str, ...] | None = None
) -> list[EmailCandidate]:
    """Generate unique candidate addresses in the requested pattern order."""
    first, last = _name_parts(contact)
    order = tuple(patterns) if patterns is not None else _DEFAULT_ORDER
    candidates: list[EmailCandidate] = []
    seen: set[str] = set()
    domain = contact.domain.strip().lower().rstrip(".")
    for pattern_id in order:
        formatter = _PATTERNS.get(pattern_id)
        if formatter is None:
            raise ValueError(f"Unknown email pattern: {pattern_id}")
        address = f"{formatter(first, last)}@{domain}"
        if address in seen:
            continue
        seen.add(address)
        candidates.append(
            EmailCandidate(
                email=address,
                contact=contact,
                source="pattern",
                pattern=pattern_id,
                evidence_tier="C",
                found_on_web=False,
                confidence=0.4,
                notes="Generated from a name pattern; unverified.",
            )
        )
    return candidates


def infer_pattern(name: str | Contact, email: str) -> str | None:
    """Return the first catalog pattern matching the address local part."""
    try:
        first, last = _name_parts(name)
    except ValueError:
        return None
    local, sep, _domain = email.strip().casefold().partition("@")
    if not sep:
        return None
    for pattern_id, formatter in _PATTERNS.items():
        if formatter(first, last) == local:
            return pattern_id
    return None


def rank_patterns(domain: str, db: sqlite3.Connection | str | Path | None = None) -> list[str]:
    """Rank a learned domain pattern first, then return the stable prior order."""
    learned: str | None = None
    owned_conn: sqlite3.Connection | None = None
    if db is not None:
        if isinstance(db, sqlite3.Connection):
            conn = db
        else:
            owned_conn = sqlite3.connect(str(db))
            conn = owned_conn
        try:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='domain_pattern'"
            ).fetchone()
            if table:
                row = conn.execute(
                    "SELECT pattern FROM domain_pattern WHERE domain=? COLLATE NOCASE",
                    (domain.strip().lower().rstrip("."),),
                ).fetchone()
                learned = row[0] if row else None
        finally:
            if owned_conn:
                owned_conn.close()
    order = list(_DEFAULT_ORDER)
    if learned in _PATTERNS:
        order.remove(learned)
        order.insert(0, learned)
    return order
