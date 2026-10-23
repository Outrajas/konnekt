"""Wellfound (formerly AngelList Talent) job source.

Wellfound is a Next.js app. Live data is published through the Apollo cache
embedded in `__NEXT_DATA__`. Each `JobListing:<id>` entry references a
`Startup:<id>` entry via `{"__ref": "Startup:..."}`. Company websites are not
published in the cache; a `<startup-slug>.wellfound` placeholder domain is used
so downstream persistence and reputation code can operate uniformly.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from leadforge.models import Job

WELLFOUND_BASE = "https://wellfound.com"
WELLFOUND_SEARCH_URL = "https://wellfound.com/jobs"

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def _extract_next_data(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _apollo_data(next_data: dict) -> dict:
    if not isinstance(next_data, dict):
        return {}
    props = next_data.get("props") or {}
    page = props.get("pageProps") or {}
    apollo_state = page.get("apolloState") or {}
    data = apollo_state.get("data") or {}
    return data if isinstance(data, dict) else {}


def _resolve(apollo: dict, value):
    if isinstance(value, dict) and "__ref" in value:
        return apollo.get(value["__ref"])
    return value


def _clean_domain(slug: str) -> str:
    """Build a stable, comparison-friendly placeholder domain from startup slug."""
    s = (slug or "").strip().lower()
    # Startup slugs on Wellfound are already safe, but guard against stray chars.
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    return f"{s}.wellfound" if s else ""


def _salary_prefix(compensation: str) -> str:
    """Prefix the description with a parseable salary line when compensation is real.

    `extract_salary` (in job_intel) will parse this string when the job is enriched,
    so Wellfound salary data flows through the same code path as ATS descriptions.
    """
    if not compensation:
        return ""
    stripped = compensation.strip()
    low = stripped.lower()
    if not stripped or low in {"undisclosed", "competitive", "doe"}:
        return ""
    return f"Salary: {stripped}. "


def _location_of(entry: dict) -> str | None:
    names = entry.get("locationNames") or []
    if isinstance(names, list) and names:
        return str(names[0])
    if entry.get("remote"):
        return "Remote"
    return None


def _job_from_apollo_entry(entry: dict, apollo: dict) -> Job | None:
    if not isinstance(entry, dict):
        return None
    title = entry.get("title")
    jid = entry.get("id")
    slug = entry.get("slug")
    if not title or not jid or not slug:
        return None

    startup = _resolve(apollo, entry.get("startup"))
    if not isinstance(startup, dict):
        return None
    company_name = startup.get("name")
    company_slug = startup.get("slug")
    if not company_name or not company_slug:
        return None

    domain = _clean_domain(str(company_slug))
    if not domain:
        return None

    url = f"{WELLFOUND_BASE}/jobs/{jid}-{slug}"
    comp = entry.get("compensation") or ""
    desc = _salary_prefix(str(comp))

    return Job(
        company=str(company_name),
        domain=domain,
        title=str(title),
        url=url,
        location=_location_of(entry),
        employment_type=None,
        description=desc,
        source="wellfound",
        remote=bool(entry.get("remote", False)),
    )


def parse_wellfound(html: str) -> list[Job]:
    """Parse Wellfound HTML. Returns [] when Apollo state is absent or unrecognised."""
    next_data = _extract_next_data(html)
    if not next_data:
        return []
    apollo = _apollo_data(next_data)
    if not apollo:
        return []

    jobs: list[Job] = []
    seen: set[str] = set()
    for key, entry in apollo.items():
        if not isinstance(key, str) or not key.startswith("JobListing:"):
            continue
        job = _job_from_apollo_entry(entry, apollo)
        if job is None or job.url in seen:
            continue
        seen.add(job.url)
        jobs.append(job)
    return jobs


def build_search_url(profile=None) -> str:
    """Default Wellfound job search URL.

    Profile-aware queries are deferred; the caller may append path segments.
    """
    return WELLFOUND_SEARCH_URL


def domain_from_wellfound_slug(domain: str) -> str:
    """Extract the Wellfound startup slug from a `.wellfound` placeholder domain."""
    if not domain or not domain.endswith(".wellfound"):
        return ""
    try:
        return urlparse(f"https://{domain}").hostname or ""
    except Exception:
        return ""

# ---------------- operator location/seniority filter ----------------

INDIA_CITIES = frozenset({
    "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad",
    "pune", "chennai", "kolkata", "ahmedabad", "noida",
    "gurgaon", "gurugram", "bhubaneswar", "jaipur", "indore",
    "chandigarh", "coimbatore", "kochi", "trivandrum",
    "thiruvananthapuram", "mysore", "mysuru", "nagpur", "surat",
    "vizag", "visakhapatnam", "vadodara", "dehradun",
})

ENTRY_LEVEL_TITLE_HINTS = frozenset({
    "junior", "jr", "entry", "graduate", "intern", "internship",
    "trainee", "fresher", "apprentice", "new grad", "recent grad",
})

SENIORITY_EXCLUDE_HINTS = frozenset({
    "senior", "sr", "staff", "principal", "lead", "head",
    "director", "vp", "vice president", "chief", "cto", "ceo",
    "founder", "co-founder", "distinguished", "architect",
    "manager", "expert",
})


def _matches_hint(text: str, hints: frozenset[str]) -> bool:
    if not text:
        return False
    low = text.lower()
    for hint in hints:
        if re.search(rf"\b{re.escape(hint)}\b", low):
            return True
    return False

def _is_india_location(loc: str) -> bool:
    if not loc:
        return False
    low = loc.lower()
    if "india" in low:
        return True
    return _matches_hint(loc, INDIA_CITIES)


def _is_entry_level_title(title: str, mode: str = "strict") -> bool:
    if not title:
        return mode == "lenient"
    if _matches_hint(title, SENIORITY_EXCLUDE_HINTS):
        return False
    if mode == "strict":
        return _matches_hint(title, ENTRY_LEVEL_TITLE_HINTS)
    return True


def filter_jobs_for_operator(
    jobs: list[Job],
    *,
    india_always_ok: bool = True,
    allow_remote_entry_level: bool = True,
    entry_level_mode: str = "strict",
) -> list[Job]:
    """Filter Wellfound jobs per the operator's location/seniority preferences.

    Rule:
      - India (any location, any seniority): accept when india_always_ok
      - Non-India: accept only when remote AND entry-level title
      - Everything else: reject

    entry_level_mode "strict" (default) requires an explicit entry keyword
    (junior, entry, graduate, intern, trainee). "lenient" accepts any title
    that is NOT explicitly senior/staff/lead/manager/etc.
    """
    out: list[Job] = []
    for job in jobs:
        loc = job.location or ""
        if india_always_ok and _is_india_location(loc):
            out.append(job)
            continue
        if allow_remote_entry_level and job.remote and _is_entry_level_title(
            job.title, entry_level_mode
        ):
            out.append(job)
            continue
    return out

