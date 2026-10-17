"""Conservative company-driven job discovery with offline planning support."""

from __future__ import annotations

import csv
from dataclasses import dataclass

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.models import Job, OperatorProfile
from leadforge.sources.ats import detect_ats_link, fetch_jobs
from leadforge.sources.careers import is_js_shell, parse_careers_page
from leadforge.sources.hn import discover_hiring_posts
from leadforge.stages.matching import match_job
from leadforge.web.errors import (
    Blocked,
    BudgetExhausted,
    DeniedHost,
    DomainPaused,
    FetchError,
    RobotsDisallowed,
)
from leadforge.web.extract import extract_links
from leadforge.web.fetcher import Fetcher
from leadforge.web.policy import DomainPolicy


@dataclass(frozen=True)
class CompanyInput:
    name: str
    domain: str


def read_companies(path) -> list[CompanyInput]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"name", "domain"}.issubset(reader.fieldnames or []):
            raise ValueError("Companies CSV must have name,domain columns")
        return [
            CompanyInput(row["name"].strip(), row["domain"].strip().lower())
            for row in reader
            if row.get("name", "").strip() and row.get("domain", "").strip()
        ]


def plan_discovery(companies: list[CompanyInput], settings: Settings) -> list[str]:
    policy = DomainPolicy.load(settings.domain_policy_path, settings)
    hosts = [company.domain for company in companies]
    lines = []
    for host in sorted(set(hosts)):
        rule = policy.resolve(host)
        lines.append(f"https://{host}/careers cap={rule.daily_cap} enabled={rule.enabled}")
    return lines


async def discover_company_jobs(
    company: CompanyInput,
    profile: OperatorProfile,
    *,
    fetcher: Fetcher | None = None,
) -> list[Job]:
    router = fetcher or Fetcher()
    for path in ("/careers", "/jobs", "/join-us", "/"):
        url = f"https://{company.domain}{path}"
        try:
            page = await router.fetch(url)
        except (Blocked, BudgetExhausted, DeniedHost, DomainPaused, RobotsDisallowed):
            raise
        except FetchError:
            continue
        links = extract_links(page.html, page.final_url)
        ats = detect_ats_link([link.url for link in links])
        if ats:
            jobs = await fetch_jobs(ats[0], ats[1], company.name, company.domain, fetcher=router)
            return [match_job(job, profile) for job in jobs]
        jobs = parse_careers_page(page.html, company.name, company.domain, page.final_url)
        if jobs:
            return [
                match_job(
                    job.model_copy(update={"fetch_tier": page.tier, "js_rendered": page.tier > 1}),
                    profile,
                )
                for job in jobs
            ]
        if is_js_shell(page.html) and page.tier > 1:
            continue
    return []


async def discover_hn_signals(companies: list[CompanyInput], profile: OperatorProfile) -> list[Job]:
    """Collect explicit hiring comments, matching company names only to supplied rows."""

    by_name = {_normalize(company.name): company for company in companies}
    posts = await discover_hiring_posts()
    matched = []
    for job in posts:
        company = by_name.get(_normalize(job.company))
        if not company:
            continue
        matched_job = job.model_copy(update={"domain": company.domain})
        matched.append(match_job(matched_job, profile))
    return matched


def persist_invite_signals(jobs: list[Job], settings: Settings) -> None:
    """Raise the local company score only for explicit HN email invitations."""
    now = clock.utcnow_iso()
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        for job in jobs:
            if not job.invites_email:
                continue
            conn.execute(
                "INSERT INTO company(name,domain,source,invite_email_score,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET "
                "invite_email_score=company.invite_email_score+1",
                (job.company, job.domain, "hacker-news", 1.0, now),
            )
        conn.commit()
    finally:
        conn.close()


def _normalize(value: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())


def save_jobs(jobs: list[Job], path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "company",
        "domain",
        "title",
        "url",
        "location",
        "employment_type",
        "source",
        "match_score",
        "match_reasons",
        "invites_email",
        "fetch_tier",
        "js_rendered",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            values = job.model_dump(mode="json")
            values["match_reasons"] = "; ".join(job.match_reasons)
            writer.writerow({key: values.get(key) for key in fields})
