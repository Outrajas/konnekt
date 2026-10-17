"""Deterministic draft checks; these checks never generate or send mail."""

from __future__ import annotations

import re

BANNED_FLATTERY = (
    "impressive background",
    "inspiring journey",
    "amazing work",
    "incredible career",
    "remarkable career",
    "exceptional talent",
    "rockstar",
    "thought leader",
    "following your work for a long time",
)
URL_PATTERN = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
SOURCE_LINK_PATTERN = re.compile(r"\[source\]\(https?://[^)]+\)", re.IGNORECASE)
QUOTED_EVIDENCE_PATTERN = re.compile(r'"([^\"]{10,})"')


def quality_issues(draft: str) -> list[str]:
    if "NEEDS_MANUAL_PERSONALIZATION" in draft:
        return ["NEEDS_MANUAL_PERSONALIZATION"]
    issues: list[str] = []
    lines = draft.splitlines()
    subject = next(
        (line.partition(":")[2].strip() for line in lines if line.lower().startswith("subject:")),
        "",
    )
    if not subject:
        issues.append("missing subject line")
    body = "\n".join(line for line in lines if not line.lower().startswith("subject:"))
    word_count = len(re.findall(r"\b[\w'-]+\b", body))
    if not 80 <= word_count <= 120:
        issues.append(f"word count {word_count} is outside 80-120")
    if (
        "professional" not in body.casefold()
        or not SOURCE_LINK_PATTERN.search(body)
        or not QUOTED_EVIDENCE_PATTERN.search(body)
        or not URL_PATTERN.search(body)
    ):
        issues.append("missing one specific professional evidence item and its URL")
    if "would you be open" not in body.casefold():
        issues.append("missing one clear ask")
    if not any(
        phrase in body.casefold()
        for phrase in ("opt out", "no further messages", "won't follow up")
    ):
        issues.append("missing plain opt-out line")
    lowered = draft.casefold()
    if any(phrase in lowered for phrase in BANNED_FLATTERY):
        issues.append("contains banned flattery")
    return issues
