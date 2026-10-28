"""End-to-end orchestrator: chain discovery -> job-intel -> people -> email -> draft.

One command that runs every stage in sequence with sensible defaults, writes
a review CSV at data/output/review_<timestamp>.csv, and prints a summary
table for human inspection.
"""
from __future__ import annotations

import csv
import logging
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
        rows = conn.execute(
            "SELECT c.name AS company, c.domain AS domain, "
            "       p.first_name, p.last_name, p.title AS person_title, "
            "       ec.email, ec.confidence, "
            "       j.title AS job_title, j.location AS job_location, "
            "       d.subject, d.body, d.draft_path "
            "FROM draft d "
            "JOIN person p ON p.id = d.person_id "
            "JOIN company c ON c.id = p.company_id "
            "LEFT JOIN email_candidate ec ON ec.person_id = p.id "
            "LEFT JOIN job j ON j.id = d.job_id "
            "WHERE (d.profile_id = ? OR d.profile_id IS NULL) "
            "ORDER BY ec.confidence DESC, c.name, p.last_name"
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
    llm_client=None,
    fetcher=None,
) -> E2EResult:
    """Chain: discover (if board) -> job-intel -> people -> email -> draft -> review.csv."""
    result = E2EResult()

    # ─── Stage A: discovery (optional - depends on --board) ──────────────
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

    # ─── Stage B: job intel (enrich new jobs) ───────────────────────────
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
                    enrich_job_row(int(jr["id"]), settings)
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

    # ─── Stage C: people ────────────────────────────────────────────────
    try:
        from leadforge.stages.people_e2e import run_people_e2e
        p_result = await run_people_e2e(
            settings,
            profile_id=profile_id,
            max_companies=max_companies,
            dry_run=dry_run,
            llm_client=llm_client,
            fetcher=fetcher,
            use_search=False,  # DDG is blocked; rely on walker + agent
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

    # ─── Stage D: email ─────────────────────────────────────────────────
    try:
        from leadforge.stages.email_e2e import run_email_e2e
        e_result = run_email_e2e(
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

    # ─── Stage E: draft ─────────────────────────────────────────────────
    try:
        from leadforge.stages.draft_e2e import run_draft_e2e
        d_result = await run_draft_e2e(
            settings,
            profile_id=profile_id,
            max_people=max_drafts,
            dry_run=dry_run,
            llm_client=llm_client,
        )
        result.stages.append(E2EStageResult(
            name="draft", ok=True,
            detail=f"{d_result.total_drafted} drafted, {d_result.total_persisted} persisted",
            metrics={
                "drafted": d_result.total_drafted,
                "persisted": d_result.total_persisted,
            },
        ))
    except Exception as exc:  # noqa: BLE001
        result.stages.append(E2EStageResult(
            name="draft", ok=False, detail=f"{type(exc).__name__}: {exc}",
        ))

    # ─── Stage F: review CSV ────────────────────────────────────────────
    if not dry_run:
        try:
            path, n = _make_review_csv(settings, profile_id)
            result.review_csv = path
            result.stages.append(E2EStageResult(
                name="review", ok=True,
                detail=f"{n} rows -> {Path(path).name}",
                metrics={"review_rows": n},
            ))
        except Exception as exc:  # noqa: BLE001
            result.stages.append(E2EStageResult(
                name="review", ok=False, detail=f"{type(exc).__name__}: {exc}",
            ))

    result.ok = all(s.ok for s in result.stages)
    return result