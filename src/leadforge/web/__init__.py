"""Polite, policy-governed web fetching."""

from dataclasses import dataclass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    html: str
    text: str
    tier: int
    from_cache: bool
    blocked: bool
    fetched_at: str
