"""Export privacy-preserving run metrics as counts only."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from leadforge.config import Settings, settings

SOURCES = (
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
    "other",
)


def _source_for_host(host: str, page_url: str = "") -> str:
    host = host.lower().rstrip(".")
    if host in {"ycombinator.com", "www.ycombinator.com"}:
        return "yc-companies" if "/companies" in page_url else "yc-jobs"
    mappings = (
        ("workatastartup.com", "workatastartup"),
        ("news.ycombinator.com", "hacker-news"),
        ("hacker-news.firebaseio.com", "hacker-news"),
        ("hn.algolia.com", "hacker-news"),
        ("greenhouse.io", "ats"),
        ("lever.co", "ats"),
        ("ashbyhq.com", "ats"),
        ("wellfound.com", "wellfound"),
        ("linkedin.com", "linkedin"),
        ("glassdoor.com", "glassdoor"),
        ("indeed.com", "indeed"),
        ("duckduckgo.com", "duckduckgo"),
    )
    for suffix, source in mappings:
        if host == suffix or host.endswith("." + suffix):
            return source
    return "other"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _seed_counts(path: Path) -> dict[str, dict[str, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    output: dict[str, dict[str, int]] = {}
    for source, metrics in payload.items():
        if source not in SOURCES or not isinstance(metrics, dict):
            continue
        output[source] = {
            key: int(metrics.get(key, 0))
            for key in ("pages", "companies", "blocks", "robots_refusals")
            if isinstance(metrics.get(key, 0), int)
        }
        for key in ("pages", "companies", "blocks", "robots_refusals"):
            output[source].setdefault(key, 0)
    return output


def _latest_enrichment_fetch_counts(output_dir: Path) -> list[int]:
    """Read only numeric fetch counts from the latest enrichment CSV."""
    paths = list(output_dir.glob("enriched_*.csv"))
    if not paths:
        return []
    try:
        latest = max(paths, key=lambda path: path.stat().st_mtime_ns)
        with latest.open("r", encoding="utf-8", newline="") as handle:
            rows = csv.DictReader(handle)
            values = []
            for row in rows:
                try:
                    value = int(row.get("fetches_used", ""))
                except (TypeError, ValueError):
                    continue
                if value >= 0:
                    values.append(value)
            return values
    except OSError:
        return []


def build_run_summary(active_settings: Settings | None = None) -> dict[str, Any]:
    """Aggregate only numeric fields and fixed source/stage labels from the local DB."""
    cfg = active_settings or settings
    source_counts: dict[str, dict[str, int]] = {
        name: {"pages": 0, "companies": 0, "blocks": 0, "robots_refusals": 0} for name in SOURCES
    }
    source_counts.update(_seed_counts(cfg.cache_dir / "seed-source-stats.json"))
    db_company_counts: dict[str, int] = {}
    stage_counts: dict[str, dict[str, int | float]] = {
        stage: {
            "people": 0,
            "tier_a": 0,
            "tier_b": 0,
            "tier_c": 0,
            "not_found": 0,
            "average_fetches": 0.0,
            "llm_calls": 0,
            "http_429s": 0,
        }
        for stage in ("discover", "people", "enrich", "report", "drafts")
    }
    db_path = cfg.db_path.resolve()
    if db_path.is_file():
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if _table_exists(conn, "page"):
                for row in conn.execute("SELECT url,host,status_code,blocked FROM page").fetchall():
                    source = _source_for_host(str(row["host"]), str(row["url"]))
                    item = source_counts[source]
                    item["pages"] += 1
                    status = row["status_code"]
                    if row["blocked"] or status in (401, 403, 429):
                        item["blocks"] += 1
            if _table_exists(conn, "company"):
                for row in conn.execute(
                    "SELECT source,COUNT(*) AS count FROM company GROUP BY source"
                ):
                    source = str(row["source"] or "other")
                    if source not in source_counts:
                        source = "other"
                    db_company_counts[source] = int(row["count"])
            people = 0
            not_found = 0
            if _table_exists(conn, "person"):
                people = int(conn.execute("SELECT COUNT(*) FROM person").fetchone()[0])
                not_found = int(
                    conn.execute("SELECT COUNT(*) FROM person WHERE status='not_found'").fetchone()[
                        0
                    ]
                )
            tiers = {"A": 0, "B": 0, "C": 0}
            enriched_people = 0
            if _table_exists(conn, "email_candidate"):
                for row in conn.execute(
                    "SELECT evidence_tier,COUNT(*) AS count FROM email_candidate "
                    "GROUP BY evidence_tier"
                ):
                    if row["evidence_tier"] in tiers:
                        tiers[str(row["evidence_tier"])] = int(row["count"])
                enriched_people = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT person_id) FROM email_candidate"
                    ).fetchone()[0]
                )
            rate_limits = 0
            robots = 0
            if _table_exists(conn, "run_log"):
                rate_limits = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM run_log WHERE stage='fetch' "
                        "AND message LIKE '%status=429%'"
                    ).fetchone()[0]
                )
                robots = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM run_log WHERE message LIKE '%robots%disallow%'"
                    ).fetchone()[0]
                )
            calls = 0
            if _table_exists(conn, "llm_usage"):
                calls = int(
                    conn.execute("SELECT COALESCE(SUM(count),0) FROM llm_usage").fetchone()[0]
                )
        finally:
            conn.close()
    else:
        people = not_found = enriched_people = rate_limits = robots = calls = 0
        tiers = {"A": 0, "B": 0, "C": 0}
    # Keep source labels fixed and count-only. Seed stats include robots refusals;
    # supplement them from safe, aggregate run-log counts where available.
    if robots:
        source_counts["other"]["robots_refusals"] += robots
    for source, count in db_company_counts.items():
        source_counts[source]["companies"] = max(source_counts[source]["companies"], count)
    fetch_counts = _latest_enrichment_fetch_counts(cfg.output_dir)
    stage_counts["people"]["people"] = people
    stage_counts["enrich"].update(
        {
            "people": enriched_people,
            "tier_a": tiers["A"],
            "tier_b": tiers["B"],
            "tier_c": tiers["C"],
            "not_found": not_found,
            "average_fetches": (
                round(sum(fetch_counts) / len(fetch_counts), 2) if fetch_counts else 0.0
            ),
            "llm_calls": calls,
            "http_429s": rate_limits,
        }
    )
    return {"sources": source_counts, "stages": stage_counts}


def export_run_summary(active_settings: Settings | None = None) -> Path:
    cfg = active_settings or settings
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    destination = cfg.output_dir / "run_summary.json"
    destination.write_text(
        json.dumps(build_run_summary(cfg), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination
