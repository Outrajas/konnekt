"""End-to-end orchestrator: chain discovery -> job-intel -> people -> email -> draft.

One command that runs every stage in sequence with sensible defaults, writes
a review CSV at data/output/review_<timestamp>.csv, and prints a summary
table for human inspection.
"""
from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.profiles import ensure_schema as ensure_profiles_schema

log = logging.getLogger(__name__)


@dataclass
class E2EStageResult:
    name: str
    ok: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)


@dataclass
class E2EResult:
    stages: list[E2EStageResult] = field(default_factory=list)
    review_csv: str = ""
    ok: bool = True


def _make_review_csv(settings: Settings, profile_id: str) -> tuple[str, int]:
    """Export the final review surface: company, person, email, subject, draft path."""
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        ensure_profiles_schema(conn)
        # One row per draft; pick the HIGHEST-confidence email per person
        rows = conn.execute(
            "SELECT c.name AS company, c.domain AS domain, "
            "       p.first_name, p.last_name, p.title AS person_title, "
            "       (SELECT email FROM email_candidate "
            "        WHERE person_id = p.id ORDER BY confidence DESC LIMIT 1) AS email, "
            "       (SELECT confidence FROM email_candidate "
            "        WHERE person_id = p.id ORDER BY confidence DESC LIMIT 1) AS confidence, "
            "       j.title AS job_title, j.location AS job_location, "
            "       d.subject, d.body, d.draft_path "
            "FROM draft d "
            "JOIN person p ON p.id = d.person_id "
            "JOIN company c ON c.id = p.company_id "
            "LEFT JOIN job j ON j.id = d.job_id "
            "WHERE (d.profile_id = ? OR d.profile_id IS NULL) "
            "ORDER BY confidence DESC, c.name, p.last_name"
        , (profile_id,)).fetchall()
    finally:
        conn.close()

    out_dir = settings.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"review_{stamp}.csv"

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "company", "domain", "person_name", "person_title",
            "job_title", "job_location", "email", "confidence",
            "subject", "draft_path", "draft_preview",
        ])
        for r in rows:
            name = f"{r['first_name']} {r['last_name']}".strip()
            body = r["body"] or ""
            preview = body[:120].replace("\n", " ")
            writer.writerow([
                r["company"] or "",
                r["domain"] or "",
                name,
                r["person_title"] or "",
                r["job_title"] or "",
                r["job_location"] or "",
                r["email"] or "",
                f"{r['confidence']:.2f}" if r["confidence"] is not None else "",
                r["subject"] or "",
                r["draft_path"] or "",
                preview,
            ])
    return str(path), len(rows)


async def run_full_e2e(
    settings: Settings,
    *,
    profile_id: str = "ai_ml",
    board: str | None = "wellfound",
    max_companies: int = 3,
    max_people: int = 5,
    max_drafts: int = 5,
    dry_run: bool = False,
    use_search: bool = False,
    use_research: bool = True,
    llm_client=None,
    fetcher=None,
    progress: Callable[[str, dict[str, object]], None] | None = None,
) -> E2EResult:
    """Chain: discover (if board) -> job-intel -> people -> email -> draft -> review.csv."""
    result = E2EResult()

    def announce(stage: str, message: str) -> None:
        if progress:
            progress(stage, {"message": message})

    # ─── Stage A: discovery (optional - depends on --board) ──────────────
    announce("stage_start", "discover")
    if board:
        try:
            from leadforge.sources.wellfound import fetch_wellfound_jobs
            wf_jobs = await fetch_wellfound_jobs(
                None, fetcher=fetcher, india=True,
            )
            if wf_jobs:
                from leadforge.job_persist import persist_jobs
                url_map, stats = persist_jobs(wf_jobs, settings, profile_id=profile_id)
                result.stages.append(E2EStageResult(
                    name="discovery",
                    ok=True,
                    detail=(
                        f"{len(wf_jobs)} jobs, "
                        f"{stats.jobs_created} new, "
                        f"{stats.jobs_updated} updated"
                    ),
                    metrics={"jobs": len(wf_jobs)},
                ))
            else:
                result.stages.append(E2EStageResult(
                    name="discovery", ok=True, detail="0 jobs from board",
                ))
        except Exception as exc:  # noqa: BLE001
            result.stages.append(E2EStageResult(
                name="discovery", ok=False, detail=f"{type(exc).__name__}: {exc}",
            ))
    else:
        result.stages.append(E2EStageResult(
            name="discovery", ok=True, detail="skipped (no board)",
        ))
    announce("stage_end", result.stages[-1].detail)

    # ─── Stage B: job intel (enrich new jobs) ───────────────────────────
    announce("stage_start", "job_intel")
    if dry_run:
        result.stages.append(E2EStageResult(
            name="job_intel", ok=True, detail="dry-run skipped",
        ))
    else:
        try:
            from leadforge.db import connect as _connect
            from leadforge.job_enrich import enrich_job_row
            from leadforge.job_reputation import compute_company_reputation

            conn = _connect(settings.db_path)
            try:
                init_schema(conn)
                job_rows = conn.execute(
                    "SELECT j.id, c.domain FROM job j JOIN company c ON c.id=j.company_id "
                    "WHERE j.profile_id=? AND j.enriched_at IS NULL "
                    "AND c.domain NOT LIKE '%.wellfound' LIMIT 20",
                    (profile_id,),
                ).fetchall()
                domains = set()
                for jr in job_rows:
                    enriched = enrich_job_row(int(jr["id"]), settings)
                    if progress and enriched is not None:
                        progress(
                            "job_enriched",
                            {
                                "job_id": int(jr["id"]),
                                "salary": enriched.salary.raw if enriched.salary else "",
                                "currency": enriched.salary.currency if enriched.salary else "",
                                "period": enriched.salary.period if enriched.salary else "",
                                "tech_count": len(enriched.tech_stack),
                            },
                        )
                    if jr["domain"]:
                        domains.add(jr["domain"])
            finally:
                conn.close()

            for d in domains:
                try:
                    compute_company_reputation(d, settings)
                except Exception:  # noqa: BLE001
                    pass

            result.stages.append(E2EStageResult(
                name="job_intel", ok=True,
                detail=f"{len(job_rows)} jobs enriched, {len(domains)} domains scored",
                metrics={"jobs_enriched": len(job_rows)},
            ))
        except Exception as exc:  # noqa: BLE001
            result.stages.append(E2EStageResult(
                name="job_intel", ok=False, detail=f"{type(exc).__name__}: {exc}",
            ))
    announce("stage_end", result.stages[-1].detail)

    # ─── Stage C: people ────────────────────────────────────────────────
    announce("stage_start", "research")
    if use_research:
        try:
            from leadforge.stages.research import ResearchBudget, run_research

            research_results = await run_research(
                settings,
                profile_id=profile_id,
                max_companies=max_companies,
                budget=ResearchBudget(
                    max_queries=4,
                    max_hits=30,
                    max_fetches=8,
                    max_llm_tokens=10_000,
                    wall_clock_seconds=180,
                ),
                dry_run=dry_run,
                progress=progress,
            )
            result.stages.append(E2EStageResult(
                name="research",
                ok=True,
                detail=(
                    f"{sum(len(item.people) for item in research_results)} people, "
                    f"{sum(len(item.emails) for item in research_results)} emails "
                    f"across {len(research_results)} companies"
                ),
                metrics={"companies": len(research_results)},
            ))
        except Exception as exc:  # noqa: BLE001
            result.stages.append(E2EStageResult(
                name="research", ok=False, detail=f"{type(exc).__name__}: {exc}",
            ))
    else:
        result.stages.append(E2EStageResult(
            name="research", ok=True, detail="skipped by configuration",
        ))
    announce("stage_end", result.stages[-1].detail)

    # ─── Stage D: people ────────────────────────────────────────────────
    announce("stage_start", "people")
    try:
        from leadforge.stages.people_e2e import run_people_e2e
        p_result = await run_people_e2e(
            settings,
            profile_id=profile_id,
            max_companies=max_companies,
            dry_run=dry_run,
            llm_client=llm_client,
            fetcher=fetcher,
            use_search=use_search,
            use_llm=llm_client is not None,
        )
        result.stages.append(E2EStageResult(
            name="people", ok=True,
            detail=f"{p_result.total_found} found, {p_result.total_persisted} new",
            metrics={"found": p_result.total_found, "persisted": p_result.total_persisted},
        ))
    except Exception as exc:  # noqa: BLE001
        result.stages.append(E2EStageResult(
            name="people", ok=False, detail=f"{type(exc).__name__}: {exc}",
        ))
    announce("stage_end", result.stages[-1].detail)

    # ─── Stage E: email ─────────────────────────────────────────────────
    announce("stage_start", "email")
    try:
        from leadforge.stages.email_e2e import run_email_e2e
        e_result = await run_email_e2e(
            settings,
            profile_id=profile_id,
            max_companies=max_companies,
            max_people_per_company=max_people,
            smtp_enabled=not dry_run,
            dry_run=dry_run,
        )
        result.stages.append(E2EStageResult(
            name="email", ok=True,
            detail=f"{e_result.total_candidates} candidates, "
                   f"{e_result.total_persisted} persisted",
            metrics={
                "candidates": e_result.total_candidates,
                "persisted": e_result.total_persisted,
            },
        ))
    except Exception as exc:  # noqa: BLE001
        result.stages.append(E2EStageResult(
            name="email", ok=False, detail=f"{type(exc).__name__}: {exc}",
        ))
    announce("stage_end", result.stages[-1].detail)

    # ─── Stage F: draft ─────────────────────────────────────────────────
    announce("stage_start", "draft")
    try:
        from leadforge.stages.draft_e2e import run_draft_e2e
        d_result = await run_draft_e2e(
            settings,
            profile_id=profile_id,
            max_people=max_drafts,
            dry_run=dry_run,
            llm_client=llm_client,
        )
        # Surface any per-person error notes so the summary is diagnostic
        notes = "; ".join(
            f"{p.name}: {p.note}" for p in d_result.people if p.note
        )
        detail = f"{d_result.total_drafted} drafted, {d_result.total_persisted} persisted"
        if notes:
            detail += f" | {notes[:200]}"
        result.stages.append(E2EStageResult(
            name="draft", ok=True,
            detail=detail,
            metrics={
                "drafted": d_result.total_drafted,
                "persisted": d_result.total_persisted,
            },
        ))
    except Exception as exc:  # noqa: BLE001
        result.stages.append(E2EStageResult(
            name="draft", ok=False, detail=f"{type(exc).__name__}: {exc}",
        ))
    announce("stage_end", result.stages[-1].detail)

    # ─── Stage G: review CSV ────────────────────────────────────────────
    if not dry_run:
        announce("stage_start", "review")
        try:
            path, n = _make_review_csv(settings, profile_id)
            result.review_csv = path
            result.stages.append(E2EStageResult(
                name="review", ok=True,
                detail=f"{n} rows -> {Path(path).name}",
                metrics={"review_rows": n},
            ))
            announce("stage_end", result.stages[-1].detail)
        except Exception as exc:  # noqa: BLE001
            result.stages.append(E2EStageResult(
                name="review", ok=False, detail=f"{type(exc).__name__}: {exc}",
            ))

    result.ok = all(s.ok for s in result.stages)
    return result