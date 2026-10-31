"""Infer seniority level from job title + description.

Values returned:
  "intern"   - internship / trainee
  "entry"    - 0-2 years, junior, fresher, graduate, associate
  "mid"      - 2-5 years, no explicit marker, or "Engineer" with 2-5 in desc
  "senior"   - "Senior", "Sr.", 5+ years
  "staff"    - "Staff", "Staff Engineer"
  "lead"     - "Lead", "Tech Lead", "Team Lead"
  "principal"- "Principal", "Distinguished"
  "exec"     - "Head of", "Director", "VP", "Chief", "CxO", "Founder"
  "unknown"  - cannot determine
"""
from __future__ import annotations

import re

# Order matters: higher = more senior. Used to pick highest match.
_TITLE_PATTERNS: list[tuple[str, list[str]]] = [
    ("exec", [
        r"\bhead\s+of\b", r"\bdirector\b", r"\bvp\b", r"\bvice\s+president\b",
        r"\bchief\b", r"\bcto\b", r"\bceo\b", r"\bcoo\b", r"\bcfo\b",
        r"\bfounder\b", r"\bco-founder\b",
    ]),
    ("principal", [
        r"\bprincipal\b", r"\bdistinguished\b", r"\bfellow\b",
    ]),
    ("staff", [r"\bstaff\b"]),
    ("lead", [
        r"\bteam\s+lead\b", r"\btech\s+lead\b", r"\bengineering\s+lead\b",
        r"\btechnical\s+lead\b", r"\blead\s+engineer\b", r"\blead\s+developer\b",
        # Broader: "Lead Software Engineer", "Lead ML Engineer", "Lead X" - safe because
        # this function is only called against titles (never descriptions where "lead"
        # is a verb like "lead the team").
        r"^lead\b", r"\blead\b",
    ]),
    ("senior", [
        r"\bsenior\b", r"\bsr\.?\b", r"\bengineer\s+iii\b", r"\bengineer\s+iv\b",
        r"\bsde\s+iii\b", r"\bsde\s+iv\b", r"\bsde\s+[3-5]\b", r"\blevel\s+iii\b",
        r"\blevel\s+(3|4|5)\b",
    ]),
    ("intern", [r"\bintern\b", r"\binternship\b", r"\btrainee\b"]),
    ("entry", [
        r"\bjunior\b", r"\bjr\.?\b", r"\bentry[\s-]level\b", r"\bfresher\b",
        r"\bnew\s+grad\b", r"\brecent\s+grad\b", r"\bgraduate\s+engineer\b",
        r"\bassociate\s+(engineer|developer)\b",
    ]),
]


def _match_title(title: str) -> str:
    if not title:
        return ""
    low = title.lower()
    for level, patterns in _TITLE_PATTERNS:
        for pat in patterns:
            if re.search(pat, low):
                return level
    return ""


def _match_description(description: str) -> str:
    """Extract years-of-experience from description text.

    Examples that parse:
      "0-2 years" -> entry
      "1+ year" -> entry
      "2-5 years" -> mid
      "3-5 years" -> mid
      "5+ years" -> senior
      "7+ years" -> senior
    """
    if not description:
        return ""
    text = re.sub(r"<[^>]+>", " ", description).lower()

    # Explicit entry markers
    if re.search(r"\b(0|zero)\s*(-|to)\s*[12]\s*years?\b", text):
        return "entry"
    if re.search(r"\bentry[\s-]level\b", text):
        return "entry"
    if re.search(r"\bfresher\b", text):
        return "entry"
    if re.search(r"\bno\s+(prior\s+)?experience\s+(required|necessary)\b", text):
        return "entry"

    # Number + optional "+" + years
    matches = re.findall(r"(\d+)\s*(?:\+\s*)?(?:-\s*(\d+)\s*)?years?\b", text)
    if matches:
        # Take the smallest "min" from the description as the seniority signal
        mins = [int(m[0]) for m in matches if m[0].isdigit()]
        if mins:
            low_min = min(mins)
            if low_min >= 8:
                return "staff"
            if low_min >= 5:
                return "senior"
            if low_min >= 3:
                return "mid"
            if low_min >= 1:
                return "entry"
            return "entry"  # 0 years
    return ""


def infer_seniority(title: str, description: str = "") -> str:
    """Return the highest-confidence seniority level for a job.

    Order: title beats description. Explicit markers beat inferred ones.
    """
    from_title = _match_title(title)
    from_desc = _match_description(description)

    if from_title and from_desc:
        # If both have signals, prefer the more senior one
        # (a "Senior" title with "3-5 years" is still senior)
        order = ["intern", "entry", "mid", "senior", "lead", "staff", "principal", "exec"]
        try:
            if order.index(from_title) >= order.index(from_desc):
                return from_title
            return from_desc
        except ValueError:
            return from_title
    return from_title or from_desc or "unknown"