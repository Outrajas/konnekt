"""Conservative company seeding from public hiring sources.

Parsing is deliberately explicit: a page without recognizable card or JSON-LD
structure is reported as unrecognised and contributes no guessed records.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import socket
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import tldextract
from bs4 import BeautifulSoup

from leadforge.config import Settings, settings
from leadforge.db import connect, init_schema
from leadforge.models import OperatorProfile
from leadforge.sources.ats import detect_ats_link, fetch_jobs
from leadforge.sources.hn import discover_hiring_posts
from leadforge.sources.search import DuckDuckGoProvider
from leadforge.web.errors import Blocked, BudgetExhausted, DomainPaused, FetchError
from leadforge.web.extract import extract_links
from leadforge.web.fetcher import Fetcher
from leadforge.web.policy import DomainPolicy

ENTRY = re.compile(r"\b(intern(?:ship)?|graduate|junior|trainee|entry[ -]level)\b", re.I)
GOOD_LOC = re.compile(r"\b(india|remote|global|anywhere)\b", re.I)
AGENCY = re.compile(r"\b(staffing|recruiting|recruitment|talent solutions)\b", re.I)
EXCLUDED = {
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "wellfound.com",
    "ycombinator.com",
    "workatastartup.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "google.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "github.com",
}


@dataclass
class ParsedListing:
    records: list[dict[str, str]] = field(default_factory=list)
    recognized: bool = False


@dataclass
class SeedCompany:
    name: str
    domain: str
    source: str
    role_title: str
    location: str
    job_url: str
    invite_email_signal: bool = False
    match_score: float = 0.0
    small_or_early: bool = False


@dataclass
class SourceStats:
    pages: int = 0
    companies: int = 0
    blocks: int = 0
    robots_refusals: int = 0
    unrecognised: int = 0


class _StatsFetcher(Fetcher):
    """Count routed requests while preserving the Fetcher policy boundary."""

    def __init__(self, delegate: Fetcher, stats: SourceStats, unavailable_hosts: set[str]) -> None:
        self.delegate = delegate
        self.stats = stats
        self.unavailable_hosts = unavailable_hosts

    async def fetch(self, url: str, *args, **kwargs):
        host = (urlsplit(url).hostname or "").lower()
        if host in self.unavailable_hosts:
            raise DomainPaused(f"{host} unavailable for this run")
        try:
            result = await self.delegate.fetch(url, *args, **kwargs)
        except (Blocked, BudgetExhausted, DomainPaused, FetchError) as exc:
            category = _fetch_error_category(exc)
            self.stats.blocks += category == "block"
            self.stats.robots_refusals += category == "robots"
            if category in {"block", "robots"} or isinstance(exc, BudgetExhausted):
                self.unavailable_hosts.add(host)
            raise
        self.stats.pages += 1
        return result


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _official_domain(url: str) -> str | None:
    domain = _domain(url)
    if not domain or domain in EXCLUDED or any(domain.endswith("." + x) for x in EXCLUDED):
        return None
    if "." not in domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
        return None
    extracted = tldextract.TLDExtract(suffix_list_urls=())(domain)
    return extracted.top_domain_under_public_suffix or None


def parse_yc_jobs(html: str, base_url: str = "") -> ParsedListing:
    return _parse_cards(html, "yc-jobs", base_url)


def parse_yc_companies(html: str, base_url: str = "") -> ParsedListing:
    return _parse_cards(html, "yc-companies", base_url)


def parse_workatastartup_jobs(html: str, base_url: str = "") -> ParsedListing:
    return _parse_cards(html, "workatastartup", base_url)


def parse_job_result_cards(html: str, source: str, base_url: str = "") -> ParsedListing:
    """Parse known semantic job cards; unsupported markup returns unrecognised."""
    if source not in {"wellfound", "linkedin", "glassdoor", "indeed"}:
        raise ValueError(f"Unsupported job-card source: {source}")
    return _parse_cards(html, source, base_url)


def _parse_cards(html: str, source: str, base_url: str = "") -> ParsedListing:
    soup = BeautifulSoup(html, "lxml")
    selectors = (
        "article[data-job-id], [data-testid='job-card'], [data-testid='job-listing'], "
        ".job-card, .job-listing, article[data-company], "
        "[data-testid='company-card'], .company-card, .startup-card"
    )
    cards = soup.select(selectors)
    # JSON-LD ItemList is a second explicit format used by public listings.
    structured: list[dict] = []
    for script in soup.select("script[type='application/ld+json']"):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = payload.get("itemListElement", []) if isinstance(payload, dict) else []
        for node in nodes if isinstance(nodes, list) else []:
            item = node.get("item", node) if isinstance(node, dict) else {}
            if isinstance(item, dict) and item.get("@type") in {"JobPosting", "Organization"}:
                structured.append(item)
    records: list[dict[str, str]] = []
    for card in cards:
        record = _card_record(card, base_url)
        if record:
            records.append(record)
    for item in structured:
        title = str(item.get("title") or item.get("name") or "").strip()
        org = item.get("hiringOrganization") or item
        name = str(org.get("name") or "").strip() if isinstance(org, dict) else ""
        url = urljoin(base_url, str(item.get("url") or ""))
        domain = _official_domain(str(org.get("url") or "")) or "" if isinstance(org, dict) else ""
        location = item.get("jobLocation") or {}
        if isinstance(location, list):
            location = location[0] if location else {}
        address = location.get("address", {}) if isinstance(location, dict) else {}
        loc = " ".join(str(address.get(key) or "") for key in ("addressLocality", "addressCountry"))
        if title and name and url.startswith(("http://", "https://")):
            records.append(
                {
                    "name": name,
                    "title": title,
                    "location": loc.strip(),
                    "url": url,
                    "domain": domain,
                }
            )
    return ParsedListing(records=records, recognized=bool(records))


def _card_record(card, base_url: str = "") -> dict[str, str] | None:
    def text(selector: str) -> str:
        node = card.select_one(selector)
        return " ".join(node.get_text(" ", strip=True).split()) if node else ""

    name = str(card.get("data-company") or card.get("data-company-name") or "").strip()
    if not name:
        name = text(
            "[data-company-name], .company-name, [itemprop='hiringOrganization'] [itemprop='name']"
        )
    title = str(card.get("data-title") or card.get("data-job-title") or "").strip() or text(
        "[data-job-title], .job-title, h2, h3, [itemprop='title']"
    )
    location = str(card.get("data-location") or "").strip() or text(
        "[data-location], .location, [itemprop='jobLocation']"
    )
    link = card.select_one("a[href]")
    url = urljoin(base_url, str(link.get("href") or "")) if link else ""
    domain = ""
    for anchor in card.select(
        "a[data-company-website], a[rel='external'], a[aria-label*='website' i]"
    ):
        candidate = _official_domain(str(anchor.get("href") or ""))
        if candidate:
            domain = candidate
            break
    if not (name and title and url.startswith(("http://", "https://"))):
        return None
    return {"name": name, "title": title, "location": location, "url": url, "domain": domain}


def parse_social_listing(html: str, source: str, base_url: str = "") -> ParsedListing:
    """Named platform entry point; no selector match is explicitly unrecognised."""
    return parse_job_result_cards(html, source, base_url)


def _fetch_error_category(exc: Exception) -> str:
    message = str(exc).lower()
    if "robots" in message or "disallow" in message:
        return "robots"
    if isinstance(exc, Blocked) or any(code in message for code in ("429", "403", "challenge")):
        return "block"
    if isinstance(exc, DomainPaused):
        return "unavailable"
    return "error"


async def _fetch_page(
    router: Fetcher,
    url: str,
    stats: SourceStats,
    *,
    purpose: str = "",
    unavailable_hosts: set[str] | None = None,
):
    host = (urlsplit(url).hostname or "").lower()
    if unavailable_hosts is not None and host in unavailable_hosts:
        return None
    try:
        page = await router.fetch(url, min_tier=1, purpose=purpose)
    except (Blocked, BudgetExhausted, DomainPaused, FetchError) as exc:
        category = _fetch_error_category(exc)
        stats.blocks += category == "block"
        stats.robots_refusals += category == "robots"
        if (
            category in {"block", "robots"} or isinstance(exc, BudgetExhausted)
        ) and unavailable_hosts is not None:
            unavailable_hosts.add(host)
        return None
    stats.pages += 1
    return page


def _role_ok(title: str, location: str) -> bool:
    return bool(ENTRY.search(title) and GOOD_LOC.search(location))


def _match_score(title: str, location: str, profile: OperatorProfile | None) -> float:
    if profile is None:
        return 0.5 if _role_ok(title, location) else 0.0
    words = set(re.findall(r"[a-z0-9]+", f"{title} {location}".lower()))
    targets = set(
        re.findall(r"[a-z0-9]+", " ".join(profile.target_titles + profile.adjacent_titles).lower())
    )
    skills = set(
        re.findall(r"[a-z0-9]+", " ".join(profile.skills + profile.adjacent_skills).lower())
    )
    title_score = len(words & targets) / max(1, len(targets))
    skill_score = len(words & skills) / max(1, len(skills))
    return min(1.0, round(0.65 * title_score + 0.35 * skill_score + 0.25, 4))


def _load_cached_pages(settings: Settings) -> list[tuple[str, str, str, str | None, bool]]:
    """Read allowed cached HTML through SQLite read-only; never creates WAL or writes."""
    db = settings.db_path.resolve()
    if not db.is_file():
        return []
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT url,host,status_code,body_path,blocked FROM page ORDER BY fetched_at"
        ).fetchall()
    finally:
        conn.close()
    root = (settings.cache_dir / "pages").resolve()
    output = []
    for url, host, status, body_path, blocked in rows:
        host = str(host).lower()
        if host not in {
            "ycombinator.com",
            "www.ycombinator.com",
            "workatastartup.com",
            "www.workatastartup.com",
        }:
            continue
        html = None
        if body_path:
            body = Path(body_path).resolve()
            if root in body.parents and body.is_file():
                try:
                    html = body.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    html = None
        output.append(
            (
                host,
                urlsplit(str(url)).path,
                html or "",
                str(status) if status is not None else None,
                bool(blocked),
            )
        )
    return output


def _write_source_stats(path: Path, stats: dict[str, SourceStats]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {key: vars(value) for key, value in stats.items()}
    path.write_text(json.dumps(safe, sort_keys=True), encoding="utf-8")


def _accepted_domain(domain: str, dns_resolver: Callable[[str], bool]) -> bool:
    domain = domain.lower().rstrip(".")
    if not domain or domain in EXCLUDED or any(domain.endswith("." + x) for x in EXCLUDED):
        return False
    try:
        return bool(dns_resolver(domain))
    except Exception:
        return False


async def seed(
    *,
    target: int = 25,
    dry_run: bool = False,
    from_cache: bool = False,
    active_settings: Settings | None = None,
    fetcher: Fetcher | None = None,
    dns_resolver: Callable[[str], bool] | None = None,
    homepage_check: Callable[[str, str], bool] | None = None,
    profile: OperatorProfile | None = None,
) -> tuple[list[SeedCompany], dict[str, SourceStats]]:
    """Run source order under the shared router; dependencies can be faked offline."""
    cfg = active_settings or settings
    router = fetcher or Fetcher(cfg)
    resolve = dns_resolver or _dns_resolves
    if profile is None:
        profile_path = cfg.data_dir / "profile.json"
        if profile_path.is_file():
            try:
                profile = OperatorProfile.model_validate_json(
                    profile_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                profile = None
    homepage_ok = homepage_check
    stats = {
        key: SourceStats()
        for key in (
            "yc-jobs",
            "yc-companies",
            "workatastartup",
            "hacker-news",
            "ats",
            "wellfound",
            "linkedin",
            "glassdoor",
            "indeed",
            "duckduckgo",
            "homepages",
        )
    }
    plan = [
        ("yc-jobs", "https://www.ycombinator.com/jobs"),
        ("yc-companies", "https://www.ycombinator.com/companies"),
        ("workatastartup", "https://www.workatastartup.com/jobs"),
        ("hacker-news", "official Who is Hiring API"),
        ("ats", "Greenhouse, Lever, Ashby boards for discovered companies"),
        ("wellfound", "one public listing page"),
        ("linkedin", "one guest listing page"),
        ("glassdoor", "one public listing page"),
        ("indeed", "one public listing page"),
        ("duckduckgo", "only missing official-domain lookups"),
    ]
    if dry_run:
        policy = DomainPolicy.load(cfg.domain_policy_path, cfg)
        hosts = {
            "yc-jobs": "www.ycombinator.com",
            "yc-companies": "www.ycombinator.com",
            "workatastartup": "www.workatastartup.com",
            "hacker-news": "hacker-news.firebaseio.com",
            "ats": "boards-api.greenhouse.io",
            "wellfound": "wellfound.com",
            "linkedin": "www.linkedin.com",
            "glassdoor": "www.glassdoor.com",
            "indeed": "www.indeed.com",
            "duckduckgo": "html.duckduckgo.com",
        }
        print("Seed fetch plan (no requests made):")
        for source, item in plan:
            rule = policy.resolve(hosts[source])
            print(
                f"  {source}: {item}; cap={rule.daily_cap}/day, max_tier={rule.max_tier}, "
                f"spacing={rule.min_delay_s:g}-{rule.max_delay_s:g}s"
            )
        return [], stats

    if not 8 <= target <= 25:
        raise ValueError("target must be between 8 and 25")

    candidates: list[SeedCompany] = []
    if from_cache:
        for host, path, html, status, blocked in _load_cached_pages(cfg):
            source = (
                "workatastartup"
                if "workatastartup" in host
                else ("yc-jobs" if path.rstrip("/").endswith("/jobs") else "yc-companies")
            )
            stat = stats[source]
            if blocked or status in {"403", "429"}:
                stat.blocks += 1
                continue
            if not html:
                stat.unrecognised += 1
                continue
            parsed = (
                parse_workatastartup_jobs(html, f"https://{host}{path}")
                if source == "workatastartup"
                else parse_yc_jobs(html, f"https://{host}{path}")
                if source == "yc-jobs"
                else parse_yc_companies(html, f"https://{host}{path}")
            )
            stat.pages += 1
            if not parsed.recognized:
                stat.unrecognised += 1
            for record in parsed.records:
                if _role_ok(record["title"], record["location"]):
                    domain = record["domain"] or ""
                    if domain:
                        candidates.append(
                            SeedCompany(
                                record["name"],
                                domain,
                                source,
                                record["title"],
                                record["location"],
                                record["url"],
                                match_score=_match_score(
                                    record["title"], record["location"], profile
                                ),
                            )
                        )
    else:
        unavailable_hosts: set[str] = set()
        first_sources = [
            ("yc-jobs", "https://www.ycombinator.com/jobs", parse_yc_jobs),
            ("yc-companies", "https://www.ycombinator.com/companies", parse_yc_companies),
            ("workatastartup", "https://www.workatastartup.com/jobs", parse_workatastartup_jobs),
        ]
        for source, url, parser in first_sources:
            page = await _fetch_page(
                router, url, stats[source], unavailable_hosts=unavailable_hosts
            )
            if page is None:
                continue
            parsed = parser(page.html, page.final_url)
            if not parsed.recognized:
                stats[source].unrecognised += 1
            for row in parsed.records:
                if _role_ok(row["title"], row["location"]):
                    candidates.append(
                        SeedCompany(
                            row["name"],
                            row["domain"],
                            source,
                            row["title"],
                            row["location"],
                            row["url"],
                            match_score=_match_score(row["title"], row["location"], profile),
                        )
                    )
        try:
            hn_router = _StatsFetcher(router, stats["hacker-news"], unavailable_hosts)
            hn_jobs = await discover_hiring_posts(
                fetcher=hn_router, max_stories=20, max_comments=60
            )
        except Exception:
            hn_jobs = []
        known = {c.name.casefold() for c in candidates}
        for job in hn_jobs:
            if job.company.casefold() in known and (
                _role_ok(job.title, job.location or "remote") or job.invites_email
            ):
                candidates.append(
                    SeedCompany(
                        job.company,
                        job.domain,
                        "hacker-news",
                        job.title,
                        job.location or "remote",
                        job.url,
                        job.invites_email,
                        _match_score(job.title, job.location or "remote", profile),
                    )
                )

        # One result/listing request per approved job platform, after source one and HN.
        platform_urls = {
            "wellfound": ("https://wellfound.com/jobs", "hiring-signal"),
            "linkedin": (
                "https://www.linkedin.com/jobs/search/?keywords=junior%20intern",
                "search-snippet-miss",
            ),
            "glassdoor": ("https://www.glassdoor.com/Job/jobs.htm", "job"),
            "indeed": ("https://www.indeed.com/jobs?q=intern+junior", "job"),
        }
        for source, (url, purpose) in platform_urls.items():
            page = await _fetch_page(
                router,
                url,
                stats[source],
                purpose=purpose,
                unavailable_hosts=unavailable_hosts,
            )
            if page is None:
                continue
            parsed = parse_social_listing(page.html, source, page.final_url)
            if not parsed.recognized:
                stats[source].unrecognised += 1
            for row in parsed.records:
                if _role_ok(row["title"], row["location"]):
                    candidates.append(
                        SeedCompany(
                            row["name"],
                            row["domain"],
                            source,
                            row["title"],
                            row["location"],
                            row["url"],
                            match_score=_match_score(row["title"], row["location"], profile),
                        )
                    )

        # Inspect only already-discovered official homepages for ATS links. The
        # homepage request itself is routed and bounded by the host's policy.
        seen_domains = {candidate.domain for candidate in candidates if candidate.domain}
        for candidate_domain in list(seen_domains):
            seed_candidate = next(
                (item for item in candidates if item.domain == candidate_domain), None
            )
            if seed_candidate is None:
                continue
            homepage = await _fetch_page(
                router,
                f"https://{candidate_domain}/",
                stats["homepages"],
                unavailable_hosts=unavailable_hosts,
            )
            if homepage is None:
                continue
            links = extract_links(homepage.html, homepage.final_url)
            ats = detect_ats_link([link.url for link in links])
            if ats:
                try:
                    ats_router = _StatsFetcher(router, stats["ats"], unavailable_hosts)
                    board_jobs = await fetch_jobs(
                        ats[0], ats[1], seed_candidate.name, candidate_domain, fetcher=ats_router
                    )
                except (Blocked, BudgetExhausted, DomainPaused, FetchError):
                    continue
                for job in board_jobs:
                    if _role_ok(job.title, job.location or ""):
                        candidates.append(
                            SeedCompany(
                                job.company,
                                candidate_domain,
                                "ats",
                                job.title,
                                job.location or "",
                                job.url,
                                match_score=_match_score(job.title, job.location or "", profile),
                            )
                        )

        # HN comments may name a new employer but omit its official domain. DDG
        # resolves only those named companies and never becomes a discovery source.
        missing_domain = [item for item in candidates if not item.domain]
        if missing_domain:
            search = DuckDuckGoProvider(fetcher=router)
            for candidate in missing_domain:
                try:
                    hits = await search.search(f"{candidate.name} official website")
                    stats["duckduckgo"].pages += 1
                except (Blocked, BudgetExhausted, DomainPaused, FetchError, ValueError):
                    stats["duckduckgo"].blocks += 1
                    continue
                for hit in hits:
                    resolved = _official_domain(hit.url)
                    if resolved and candidate.name.casefold() in hit.title.casefold():
                        candidate.domain = resolved
                        break

    # Conservative acceptance: the domain must resolve and an operator-provided
    # homepage checker must confirm the name. Missing checks reject, never guess.
    accepted: list[SeedCompany] = []
    by_domain: dict[str, SeedCompany] = {}
    for candidate in candidates:
        domain = candidate.domain.lower().strip().rstrip(".")
        if (
            not candidate.name
            or AGENCY.search(candidate.name)
            or AGENCY.search(candidate.role_title)
            or not domain
            or not _accepted_domain(domain, resolve)
        ):
            continue
        if homepage_ok is not None:
            homepage_valid = homepage_ok(domain, candidate.name)
        elif from_cache:
            homepage_valid = False
        else:
            homepage = await _fetch_page(
                router,
                f"https://{domain}/",
                stats["homepages"],
                unavailable_hosts=unavailable_hosts,
            )
            homepage_text = (
                BeautifulSoup(homepage.html, "lxml").get_text(" ", strip=True).casefold()
                if homepage
                else ""
            )
            homepage_valid = candidate.name.casefold() in homepage_text and not AGENCY.search(
                homepage_text
            )
        if not homepage_valid:
            continue
        old = by_domain.get(domain)
        if old is None:
            by_domain[domain] = candidate
        else:
            old.invite_email_signal |= candidate.invite_email_signal
            if candidate.match_score > old.match_score:
                old.role_title, old.location, old.job_url = (
                    candidate.role_title,
                    candidate.location,
                    candidate.job_url,
                )
                old.match_score = candidate.match_score
    accepted = list(by_domain.values())
    accepted.sort(key=lambda row: (not row.invite_email_signal, -row.match_score, row.domain))
    accepted = _prefer_early_stage(accepted, target)
    if len(accepted) < 8:
        for key, stat in stats.items():
            stat.companies = len({c.domain for c in accepted if c.source == key})
        _write_source_stats(cfg.cache_dir / "seed-source-stats.json", stats)
        return [], stats
    if from_cache:
        output_cfg = _isolated_cache_settings(cfg)
    else:
        output_cfg = cfg
    _write_outputs(accepted, output_cfg, target)
    if accepted:
        _persist_jobs(accepted, output_cfg)
    for key, stat in stats.items():
        stat.companies = len({c.domain for c in accepted if c.source == key})
    _write_source_stats(output_cfg.cache_dir / "seed-source-stats.json", stats)
    return accepted[:target], stats


def _dns_resolves(domain: str) -> bool:
    return bool(socket.getaddrinfo(domain, None))


def _prefer_early_stage(items: list[SeedCompany], target: int) -> list[SeedCompany]:
    # Marking early-stage is based on explicit source provenance, not a guessed
    # company-size claim. YC/Work at a Startup entries qualify as small/early.
    def is_early(item: SeedCompany) -> bool:
        return item.source in {"yc-jobs", "yc-companies", "workatastartup"}

    selected = items[:target]
    required = min(sum(is_early(item) for item in items), (target + 1) // 2)
    while sum(is_early(item) for item in selected) < required:
        replacement = next(
            (item for item in items if item not in selected and is_early(item)), None
        )
        removable = next((item for item in reversed(selected) if not is_early(item)), None)
        if replacement is None or removable is None:
            break
        selected.remove(removable)
        selected.append(replacement)
    selected.sort(key=lambda row: (not row.invite_email_signal, -row.match_score, row.domain))
    return selected


def _isolated_cache_settings(cfg: Settings) -> Settings:
    return cfg.model_copy(update={"db_path": cfg.cache_dir / "seed-replay.db"})


def _write_outputs(items: list[SeedCompany], cfg: Settings, target: int) -> None:
    cfg.input_dir.mkdir(parents=True, exist_ok=True)
    with (cfg.input_dir / "companies.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "domain"])
        for item in items[:target]:
            writer.writerow([item.name, item.domain.lower()])
    with (cfg.input_dir / "companies_meta.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "name",
                "domain",
                "source",
                "role_title",
                "location",
                "job_url",
                "invite_email_signal",
                "match_score",
            ]
        )
        for item in items[:target]:
            writer.writerow(
                [
                    item.name,
                    item.domain.lower(),
                    item.source,
                    item.role_title,
                    item.location,
                    item.job_url,
                    str(item.invite_email_signal).lower(),
                    f"{item.match_score:.4f}",
                ]
            )


def _persist_jobs(items: list[SeedCompany], cfg: Settings) -> None:
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        now = datetime.now(UTC).isoformat()
        for item in items:
            conn.execute(
                "INSERT INTO company(name,domain,source,invite_email_score,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET "
                "name=excluded.name,source=excluded.source",
                (item.name, item.domain, item.source, float(item.invite_email_signal), now),
            )
            company_row = conn.execute(
                "SELECT id FROM company WHERE domain=?", (item.domain,)
            ).fetchone()
            assert company_row is not None
            company_id = company_row[0]
            conn.execute(
                "INSERT INTO job(company_id,title,url,location,source,match_score,"
                "first_seen,last_seen) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                "match_score=excluded.match_score,last_seen=excluded.last_seen",
                (
                    company_id,
                    item.role_title,
                    item.job_url,
                    item.location,
                    item.source,
                    item.match_score,
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def seed_sync(**kwargs):
    return asyncio.run(seed(**kwargs))
