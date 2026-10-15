"""Spec-defined email confidence scoring and outreach eligibility."""

from __future__ import annotations

from typing import Literal

from leadforge.config import Settings
from leadforge.models import EmailCandidate, VerificationVerdict

EvidenceTier = Literal["A", "B", "C"]


def evidence_tier(
    candidate: EmailCandidate, *, pattern_matches_domain: bool = False
) -> EvidenceTier:
    """Classify evidence using the plan's authorship and established-pattern rules."""
    if candidate.evidence_tier == "A":
        return "A"
    if (
        pattern_matches_domain
        and candidate.verification_method.value == "smtp"
        and candidate.verification_verdict is VerificationVerdict.SEND
        and candidate.catch_all_domain is not True
    ):
        return "B"
    return "C"


def score(
    candidate: EmailCandidate,
    *,
    source_count: int,
    pattern_matches_domain: bool,
) -> float:
    """Apply the original specification's confidence weights exactly."""
    value = 0.0
    if candidate.verification_verdict is VerificationVerdict.SEND:
        value += 0.40
    if candidate.found_on_web:
        value += 0.25
    if source_count >= 2:
        value += 0.15
    if pattern_matches_domain:
        value += 0.10
    if candidate.catch_all_domain is True:
        value -= 0.20
    value = min(1.0, max(0.0, value))
    if (
        candidate.verification_method.value == "pattern_only"
        and candidate.verification_verdict is not VerificationVerdict.SEND
    ):
        value = min(value, 0.40)
    if evidence_tier(candidate, pattern_matches_domain=pattern_matches_domain) == "C":
        value = min(value, 0.40)
    return round(value, 10)


def is_sendable(candidate: EmailCandidate, settings: Settings) -> bool:
    """Require confidence, eligible evidence tier, a non-kill verdict, and safe catch-all status."""
    tier = candidate.evidence_tier
    return (
        candidate.confidence >= settings.min_confidence_to_emit
        and tier in {"A", "B"}
        and candidate.verification_verdict is not VerificationVerdict.KILL
        and not (candidate.catch_all_domain is True and tier != "A")
    )
