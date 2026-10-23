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
    """Prefix the description with a parseable salary line.

    `extract_salary` will parse this string during enrichment, so Wellfound
    salary data flows through the same code path as ATS descriptions. The
    equity portion after a bullet character is stripped so the salary regex
    stays clean.
    """
    if not compensation:
        return ""
    stripped = compensation.strip()
    if "\u2022" in stripped:
        stripped = stripped.split("\u2022", 1)[0].strip()
    low = stripped.lower()
    if not stripped or low in {
        "undisclosed",
        "competitive",
        "doe",
        "no equity",
        "equity only",
    }:
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


def _job_from_search_result(entry: dict, startup: dict) -> Job | None:
    """Build a Job from a role-page JobListingSearchResult + its StartupResult."""
    if not isinstance(entry, dict) or not isinstance(startup, dict):
        return None
    title = entry.get("title") or entry.get("primaryRoleTitle") or ""
    jid = entry.get("id")
    slug = entry.get("slug")
    company = startup.get("name")
    company_slug = startup.get("slug")
    if not (title and jid and slug and company and company_slug):
        return None
    domain = _clean_domain(str(company_slug))
    if not domain:
        return None
    url = f"{WELLFOUND_BASE}/jobs/{jid}-{slug}"
    comp = entry.get("compensation") or ""
    desc = _salary_prefix(str(comp))
    locs = entry.get("locationNames") or []
    if isinstance(locs, list) and locs:
        location = str(locs[0])
    elif entry.get("remote"):
        location = "Remote"
    else:
        location = None
    return Job(
        company=str(company),
        domain=domain,
        title=str(title),
        url=url,
        location=location,
        employment_type=entry.get("jobType"),
        description=desc,
        source="wellfound",
        remote=bool(entry.get("remote", False)),
    )


def _walk_apollo_search(apollo: dict) -> list[Job]:
    """Role pages: iterate StartupResult, resolve highlightedJobListings refs."""
    jobs: list[Job] = []
    seen: set[str] = set()
    for key, startup in apollo.items():
        if not isinstance(key, str) or not key.startswith("StartupResult:"):
            continue
        if not isinstance(startup, dict):
            continue
        refs = startup.get("highlightedJobListings") or []
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict) or "__ref" not in ref:
                continue
            entry = apollo.get(ref["__ref"])
            if not isinstance(entry, dict):
                continue
            job = _job_from_search_result(entry, startup)
            if job is not None and job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
    return jobs


def parse_wellfound(html: str) -> list[Job]:
    """Parse Wellfound HTML. Dispatches on Apollo entry types.

    - Homepage (`/jobs`): `JobListing:*` + `Startup:*`
    - Role pages (`/role/...`): `StartupResult:*` + `JobListingSearchResult:*`
    """
    next_data = _extract_next_data(html)
    if not next_data:
        return []
    apollo = _apollo_data(next_data)
    if not apollo:
        return []

    has_search_results = any(
        isinstance(k, str) and k.startswith("JobListingSearchResult:")
        for k in apollo
    )
    if has_search_results:
        return _walk_apollo_search(apollo)

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


def build_search_url(profile=None, *, india: bool = True) -> str:
    """Build a Wellfound search URL.

    - No profile -> the India role page for machine-learning-engineer (default
      operator track)
    - Profile with target_titles -> role page for the first title
    - india=True -> `/role/l/<role>/india` (location-scoped)
    - india=False -> `/role/r/<role>` (global remote)
    """
    default_role = "machine-learning-engineer"
    if profile is None or not getattr(profile, "target_titles", None):
        role = default_role
    else:
        raw = profile.target_titles[0].lower().strip()
        slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
        role = slug or default_role
    if india:
        return f"{WELLFOUND_BASE}/role/l/{role}/india"
    return f"{WELLFOUND_BASE}/role/r/{role}"


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

# ---------------- fetcher ----------------


async def fetch_wellfound_jobs(
    profile=None,
    *,
    fetcher=None,
    india: bool = True,
    entry_level_mode: str = "lenient",
) -> list[Job]:
    """Fetch and parse Wellfound role-page jobs.

    Scrapling's Tier 1 hangs on wellfound.com, so this fetcher calls the
    router with force_httpx=True. The parser, filter, and downstream
    pipeline are unchanged.

    Returns an empty list on any fetch or parse failure; callers should not
    assume success.
    """
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    url = build_search_url(profile, india=india)
    try:
        resp = await router.fetch(
            url,
            min_tier=1,
            max_tier=1,
            purpose="job",
            force_httpx=True,
        )
    except FetchError:
        return []
    jobs = parse_wellfound(resp.html or "")
    return filter_jobs_for_operator(jobs, entry_level_mode=entry_level_mode)

