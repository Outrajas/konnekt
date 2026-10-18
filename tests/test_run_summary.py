import json
import re

from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.stages.run_summary import export_run_summary


def test_run_summary_contains_counts_only_and_no_url_queries(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cfg = Settings(
        data_dir=tmp_path / "data",
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        cache_dir=cache,
        db_path=cache / "fixture.db",
    )
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO company(name,domain,source,created_at) VALUES(?,?,?,?)",
        ("Fictional Acorn", "fictional-acorn.invalid", "yc-jobs", "2026-01-01T00:00:00Z"),
    )
    company_id = conn.execute("SELECT id FROM company").fetchone()[0]
    conn.execute(
        "INSERT INTO person(company_id,first_name,last_name,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            company_id,
            "Faye",
            "Example",
            "not_found",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    person_id = conn.execute("SELECT id FROM person").fetchone()[0]
    conn.execute(
        "INSERT INTO email_candidate(person_id,email,source,evidence_tier,created_at) "
        "VALUES(?,?,?,?,?)",
        (person_id, "faye@example.invalid", "fictional", "A", "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO page(url,url_hash,host,tier,status_code,blocked,fetched_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "https://fictional.invalid/page?token=fictional",
            "fictional-hash",
            "fictional.invalid",
            1,
            429,
            1,
            "2026-01-01T00:00:00Z",
            "2027-01-01T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO run_log(ts,stage,level,message) VALUES(?,?,?,?)",
        ("2026-01-01T00:00:00Z", "fetch", "info", "status=429 host=fictional.invalid"),
    )
    conn.commit()
    conn.close()
    (cache / "seed-source-stats.json").write_text(
        json.dumps({"yc-jobs": {"pages": 2, "companies": 1, "blocks": 0, "robots_refusals": 0}}),
        encoding="utf-8",
    )

    path = export_run_summary(cfg)
    content = path.read_text(encoding="utf-8")
    payload = json.loads(content)
    assert "@" not in content
    assert re.search(r"https?://[^\s\"']+\?", content) is None
    assert "Faye" not in content and "fictional-acorn" not in content
    assert payload["sources"]["yc-jobs"]["companies"] == 1
    assert payload["sources"]["other"]["blocks"] == 1
    assert payload["stages"]["people"]["people"] == 1
    assert payload["stages"]["enrich"]["tier_a"] == 1
    assert payload["stages"]["enrich"]["not_found"] == 1
    assert payload["stages"]["enrich"]["http_429s"] == 1
