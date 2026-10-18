import asyncio
import hashlib
import sqlite3

from leadforge.config import Settings
from leadforge.stages.seed_companies import (
    SeedCompany,
    _prefer_early_stage,
    parse_job_result_cards,
    parse_workatastartup_jobs,
    seed,
)

FICTIONAL_YC = """<!doctype html><main>
<article data-job-id="fixture-1" data-company="Fable Systems" data-title="Junior Analyst"
         data-location="Remote - India">
  <a href="https://jobs.example.com/fable/1">Junior Analyst</a>
  <a rel="external" href="https://fable-systems.co.in/" aria-label="Company website">site</a>
</article></main>"""
FICTIONAL_WAS = """<!doctype html><section>
<div class="job-card" data-company-name="Paper Kite" data-job-title="Graduate Engineer"
     data-location="India">
  <a href="https://jobs.example.com/paper/2">Graduate Engineer</a>
  <a data-company-website href="https://paperkite.co.in/">website</a>
</div></section>"""
FICTIONAL_UNRECOGNISED = (
    "<html><body><div>Nothing that identifies a listing card.</div></body></html>"
)


def test_known_semantic_job_cards_parse_without_guessing_domains():
    parsed = parse_job_result_cards(FICTIONAL_YC, "linkedin")
    assert parsed.recognized
    assert parsed.records == [
        {
            "name": "Fable Systems",
            "title": "Junior Analyst",
            "location": "Remote - India",
            "url": "https://jobs.example.com/fable/1",
            "domain": "fable-systems.co.in",
        }
    ]


def test_workatastartup_structure_and_unrecognised_markup():
    parsed = parse_workatastartup_jobs(FICTIONAL_WAS)
    assert parsed.recognized
    assert parsed.records[0]["name"] == "Paper Kite"
    assert parsed.records[0]["title"] == "Graduate Engineer"
    unknown = parse_workatastartup_jobs(FICTIONAL_UNRECOGNISED)
    assert not unknown.recognized
    assert unknown.records == []


def test_early_stage_mix_preserves_global_ranking_order():
    rows = [
        SeedCompany(
            name=f"Fictional {index}",
            domain=f"fictional{index}.invalid",
            source="hn" if index < 8 else "yc-jobs",
            role_title="Junior Engineer",
            location="Remote",
            job_url=f"https://fictional{index}.invalid/jobs/{index}",
            invite_email_signal=index in {0, 8},
            match_score=1.0 - index / 20,
        )
        for index in range(13)
    ]
    ranked = sorted(
        rows,
        key=lambda item: (not item.invite_email_signal, -item.match_score, item.domain),
    )
    selected = _prefer_early_stage(ranked, 8)
    assert len(selected) == 8
    assert sum(item.source == "yc-jobs" for item in selected) >= 4
    assert selected == sorted(
        selected,
        key=lambda item: (not item.invite_email_signal, -item.match_score, item.domain),
    )


def test_dry_run_never_calls_fetcher_or_writes_outputs(tmp_path):
    cfg = Settings(
        data_dir=tmp_path / "data",
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "cache" / "real.db",
    )

    class NeverFetch:
        async def fetch(self, *args, **kwargs):
            raise AssertionError("dry-run attempted a request")

    companies, stats = asyncio.run(seed(dry_run=True, active_settings=cfg, fetcher=NeverFetch()))
    assert companies == []
    assert stats["yc-jobs"].pages == 0
    assert not (cfg.input_dir / "companies.csv").exists()


def test_from_cache_reads_db_read_only_and_writes_only_replay_database(tmp_path):
    cache = tmp_path / "cache"
    pages = cache / "pages" / "ab"
    pages.mkdir(parents=True)
    html_path = pages / "fictional.html"
    html_path.write_text(FICTIONAL_YC, encoding="utf-8")
    db = cache / "leadforge.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE page (id INTEGER PRIMARY KEY, url TEXT, host TEXT, status_code INTEGER, "
        "body_path TEXT, blocked INTEGER, fetched_at TEXT)"
    )
    conn.execute(
        "INSERT INTO page(url,host,status_code,body_path,blocked,fetched_at) VALUES(?,?,?,?,?,?)",
        ("https://www.ycombinator.com/jobs", "www.ycombinator.com", 200, str(html_path), 0, "now"),
    )
    conn.commit()
    conn.close()
    before = hashlib.sha256(db.read_bytes()).digest()
    cfg = Settings(
        data_dir=tmp_path / "data",
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        cache_dir=cache,
        db_path=db,
    )
    _, stats = asyncio.run(seed(from_cache=True, active_settings=cfg, dns_resolver=lambda _: True))
    assert stats["yc-jobs"].pages == 1
    assert hashlib.sha256(db.read_bytes()).digest() == before
    assert (cache / "seed-replay.db").is_file() is False  # no accepted row, so no writes
    assert parse_workatastartup_jobs(FICTIONAL_UNRECOGNISED).recognized is False
