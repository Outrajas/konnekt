"""Deterministic job intelligence: salary + tech-stack extraction.

Pure functions only. No network, no database. The reputation scorer will
live in a separate module so this one stays offline-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from leadforge.stages.profile import _SKILL_PATTERNS as _PROFILE_SKILL_PATTERNS

# ---------------- salary ----------------


@dataclass(frozen=True)
class SalaryInfo:
    min_amount: int
    max_amount: int
    currency: str
    period: str  # "year" | "month" | "hour" | "day"
    raw: str


_CURRENCY_RE = {
    "USD": (r"\$", r"USD", r"US\$"),
    "INR": (r"₹", r"INR", r"Rs\.?", r"\brupees?\b"),
    "EUR": (r"€", r"EUR"),
    "GBP": (r"£", r"GBP"),
    "JPY": (r"¥", r"JPY"),
    "CAD": (r"C\$", r"CAD"),
    "AUD": (r"A\$", r"AUD"),
}

_CURRENCY_TOKENS = r"[$₹€£¥]|USD|INR|EUR|GBP|JPY|CAD|AUD|Rs\.?|rupees"

_RANGE_SEP = r"(?:\s*(?:-|–|—|to)\s*)"
_NUM = r"\d[\d,.]*[kKmM]?"

_SALARY_RANGE_RE = re.compile(
    rf"(?P<cur1>{_CURRENCY_TOKENS})\s*(?P<n1>{_NUM})"
    rf"{_RANGE_SEP}"
    rf"(?P<cur2>{_CURRENCY_TOKENS})?\s*(?P<n2>{_NUM})",
    re.IGNORECASE,
)

_LPA_RANGE_RE = re.compile(
    r"(?P<n1>\d[\d,.]*)\s*(?:-|–|to)\s*(?P<n2>\d[\d,.]*)\s*LPA",
    re.IGNORECASE,
)

_SINGLE_SALARY_RE = re.compile(
    rf"(?P<cur>{_CURRENCY_TOKENS})\s*(?P<n>{_NUM})",
    re.IGNORECASE,
)

_YEAR_RE = re.compile(
    r"per\s+year|annually|annual|yearly|per\s+annum|p\.?a\.?|\ba\s+year\b|/year",
    re.I,
)
_MONTH_RE = re.compile(r"per\s+month|monthly|\ba\s+month\b|/month", re.I)
_HOUR_RE = re.compile(r"per\s+hour|hourly|\bper\s+hr\b|/hour|/\s*hr\b", re.I)
_DAY_RE = re.compile(r"per\s+day|daily|/day", re.I)

_PERIOD_PATTERNS = (
    (_YEAR_RE, "year"),
    (_MONTH_RE, "month"),
    (_HOUR_RE, "hour"),
    (_DAY_RE, "day"),
)


def _detect_period(text: str) -> str:
    for pat, period in _PERIOD_PATTERNS:
        if pat.search(text):
            return period
    return ""


def _currency_of(token: str) -> str:
    token = token or ""
    tl = token.lower()
    for code, patterns in _CURRENCY_RE.items():
        for pat in patterns:
            if re.fullmatch(pat, token, re.I):
                return code
    if tl in {"rs", "rs.", "rupees", "rupee"}:
        return "INR"
    return ""


def _to_int(number: str) -> int | None:
    """Parse a salary number: '120,000', '120k', '1.2M', '120000'."""
    if not number:
        return None
    s = number.strip().lower()
    mult = 1
    if s.endswith("k"):
        mult = 1_000
        s = s[:-1]
    elif s.endswith("m"):
        mult = 1_000_000
        s = s[:-1]
    s = s.replace(",", "")
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return None


def _to_float(number: str) -> float | None:
    if not number:
        return None
    s = number.strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def extract_salary(text: str) -> SalaryInfo | None:
    """Best-effort salary extraction. Returns None if not found."""
    if not text:
        return None
    period = _detect_period(text)

    # 1. LPA range: "12-15 LPA" -> INR, year
    m = _LPA_RANGE_RE.search(text)
    if m:
        lo = _to_float(m.group("n1"))
        hi = _to_float(m.group("n2"))
        if lo is not None and hi is not None:
            return SalaryInfo(
                min_amount=int(round(lo * 100_000)),
                max_amount=int(round(hi * 100_000)),
                currency="INR",
                period="year",
                raw=m.group(0),
            )

    # 2. Symbol range: "$120k-$180k" or "£70k - £90k"
    m = _SALARY_RANGE_RE.search(text)
    if m:
        lo = _to_int(m.group("n1"))
        hi = _to_int(m.group("n2"))
        cur = _currency_of(m.group("cur1")) or _currency_of(m.group("cur2") or "")
        if lo is not None and hi is not None and lo > 0 and hi >= lo:
            return SalaryInfo(
                min_amount=lo,
                max_amount=hi,
                currency=cur,
                period=period or "year",
                raw=m.group(0),
            )

    # 3. Single value: "$120,000" or "£70k"
    m = _SINGLE_SALARY_RE.search(text)
    if m:
        val = _to_int(m.group("n"))
        cur = _currency_of(m.group("cur"))
        if val is not None and val > 0:
            return SalaryInfo(
                min_amount=val,
                max_amount=val,
                currency=cur,
                period=period or "year",
                raw=m.group(0),
            )

    return None


# ---------------- tech stack ----------------


_JOB_TECH_EXTRA: set[str] = {
    # Infra / devops
    "microservices", "event-driven", "sre", "observability",
    "kubernetes", "k8s", "helm", "argocd", "circleci",
    "github actions", "gitlab ci", "jenkins", "travis",
    # Data infra
    "elasticsearch", "redis", "prometheus", "grafana", "datadog",
    "kinesis", "redshift", "athena", "emr", "glue",
    "dynamodb", "cassandra", "neo4j", "sql server", "sqlite",
    # Cloud services
    "lambda", "s3", "ec2", "rds", "cloudfront", "sagemaker",
    # ML ops
    "kubeflow", "mlflow", "feature store", "a/b testing",
    # Vector / LLM
    "pinecone", "weaviate", "milvus", "qdrant", "chroma",
    "openai", "anthropic", "huggingface", "ollama",
    "stable diffusion", "diffusers", "llamaindex",
}


def _compile_extra(skills: set[str]) -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for skill in skills:
        esc = re.escape(skill)
        left = r"(?<![A-Za-z0-9])" if skill[0].isalnum() else ""
        right = r"(?![A-Za-z0-9])" if skill[-1].isalnum() else ""
        out.append((skill, re.compile(left + esc + right)))
    return out


_TECH_PATTERNS: list[tuple[str, re.Pattern[str]]] = (
    list(_PROFILE_SKILL_PATTERNS) + _compile_extra(_JOB_TECH_EXTRA)
)


def extract_tech_stack(description: str, title: str = "") -> list[str]:
    """Return sorted unique tech skills found in description + title."""
    combined = (title + "\n" + description).lower()
    if not combined.strip():
        return []
    return sorted({skill for skill, pat in _TECH_PATTERNS if pat.search(combined)})