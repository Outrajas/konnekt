from email.message import EmailMessage
from pathlib import Path

from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.stages.import_pages import (
    parse_saved_page,
    parse_saved_page_with_llm,
    persist_import,
    saved_page_paths,
)

FIXTURES = Path("tests/fixtures/saved_pages")


def test_parses_fictional_page_kinds_and_drops_unrelated_fields() -> None:
    person = parse_saved_page(FIXTURES / "linkedin-person.html")
    assert (person.kind, person.status, person.name, person.company) == (
        "linkedin",
        "recognized",
        "Jordan Rivera",
        "Northstar Labs",
    )
    assert person.title == "Staff Engineer at Northstar Labs"
    assert all("email" not in key and "phone" not in key for key, _value in person.evidence)

    job = parse_saved_page(FIXTURES / "indeed-job.html")
    assert (job.kind, job.job_title, job.company, job.location) == (
        "indeed_job",
        "Backend Engineer",
        "Northstar Labs",
        "Pune, India",
    )
    assert job.posting_url == "https://www.indeed.com/viewjob?jk=fictional"

    glassdoor = parse_saved_page(FIXTURES / "glassdoor-company.html")
    assert (glassdoor.kind, glassdoor.company) == ("glassdoor_company", "Northstar Labs")
    generic = parse_saved_page(FIXTURES / "generic-company.html")
    assert (generic.kind, generic.company) == ("company", "Cedar Works")


def test_unrecognised_markup_is_not_guessed() -> None:
    page = parse_saved_page(FIXTURES / "unknown.html")
    assert page.status == "unrecognised page"
    assert page.name is None and page.company is None and page.job_title is None


def test_mhtml_is_supported(tmp_path) -> None:
    message = EmailMessage()
    message.set_content(
        '<html><head><title>Role - Indeed</title><meta property="og:site_name" '
        'content="Indeed"><link rel="canonical" href="https://indeed.com/jobs/view/fictional"></head>'
        '<body><h1>Support Engineer</h1><div class="companyName">Cedar Works</div></body></html>',
        subtype="html",
    )
    path = tmp_path / "saved.mhtml"
    path.write_bytes(message.as_bytes())
    page = parse_saved_page(path)
    assert page.kind == "indeed_job"
    assert page.job_title == "Support Engineer"
    assert page.company == "Cedar Works"


def test_txt_saved_page_uses_explicit_work_field_labels(tmp_path) -> None:
    path = tmp_path / "role.txt"
    path.write_text(
        "Indeed job listing\nJob title: Reliability Engineer\nCompany: Cedar Works\n"
        "Location: Remote\nPosting URL: https://indeed.com/jobs/view/fictional\n",
        encoding="utf-8",
    )
    page = parse_saved_page(path)
    assert page.kind == "indeed_job"
    assert page.job_title == "Reliability Engineer"
    assert page.company == "Cedar Works"
    assert page.location == "Remote"
    assert page.posting_url == "https://indeed.com/jobs/view/fictional"


async def test_llm_fallback_only_fills_ambiguous_fields_and_cross_checks(tmp_path) -> None:
    path = tmp_path / "ambiguous.html"
    path.write_text(
        '<html><head><title>Indeed listing</title><meta property="og:site_name" '
        'content="Indeed"></head><body><p>Cloud Engineer at Northstar Labs, Remote</p>'
        '<a href="https://indeed.com/jobs/view/llm-fixture">Apply</a></body></html>',
        encoding="utf-8",
    )
    calls = []

    async def grounded_llm(**kwargs):
        calls.append(kwargs["page_kind"])
        return {
            "kind": "indeed_job",
            "job_title": "Cloud Engineer",
            "company": "Northstar Labs",
            "location": "Remote",
            "posting_url": "https://indeed.com/jobs/view/llm-fixture",
        }

    parsed = await parse_saved_page_with_llm(path, grounded_llm)
    assert calls == ["indeed_job"]
    assert parsed.status == "recognized"
    assert parsed.job_title == "Cloud Engineer"

    async def hallucinated_llm(**kwargs):
        return {"kind": "indeed_job", "job_title": "Made Up Role", "company": "Northstar Labs"}

    rejected = await parse_saved_page_with_llm(path, hallucinated_llm)
    assert rejected.status == "unrecognised page"
    assert rejected.job_title is None


def test_saved_page_paths_only_include_supported_files(tmp_path) -> None:
    (tmp_path / "one.html").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / "two.txt").write_text("text", encoding="utf-8")
    (tmp_path / "three.mhtml").write_text("mime", encoding="utf-8")
    (tmp_path / "four.pdf").write_bytes(b"pdf")
    assert [path.name for path in saved_page_paths(tmp_path)] == [
        "one.html",
        "three.mhtml",
        "two.txt",
    ]


def test_persisted_manual_evidence_is_professional_and_work_only(tmp_path) -> None:
    path = FIXTURES / "indeed-job.html"
    page = parse_saved_page(path)
    db_path = tmp_path / "import.db"
    settings = Settings(_env_file=None, db_path=db_path)
    persist_import(page, settings)
    conn = connect(db_path)
    try:
        init_schema(conn)
        job = conn.execute("SELECT title,location,url FROM job").fetchone()
        evidence = conn.execute("SELECT kind,value,professional FROM evidence").fetchall()
        assert tuple(job) == (
            "Backend Engineer",
            "Pune, India",
            "https://www.indeed.com/viewjob?jk=fictional",
        )
        assert evidence
        assert all(row["professional"] == 1 for row in evidence)
        assert all("555-0100" not in row["value"] for row in evidence)
        assert all("personal" not in row["kind"] for row in evidence)
    finally:
        conn.close()


def test_cli_dry_run_is_offline_and_delete_after_removes_imported_files(
    tmp_path, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from leadforge import cli

    saved = tmp_path / "input" / "saved_pages"
    saved.mkdir(parents=True)
    target = saved / "job.html"
    target.write_bytes((FIXTURES / "indeed-job.html").read_bytes())
    monkeypatch.setattr(cli.settings, "input_dir", saved.parent)
    monkeypatch.setattr(cli.settings, "db_path", tmp_path / "pages.db")

    class NoNetwork:
        def __init__(self, *args, **kwargs):
            raise AssertionError("import-pages must not create a fetcher")

    monkeypatch.setattr(cli, "Fetcher", NoNetwork)
    runner = CliRunner()
    preview = runner.invoke(cli.app, ["import-pages", "--dry-run", "--delete-after"])
    assert preview.exit_code == 0
    assert "Would import 1 of 1" in preview.output
    assert target.exists()
    assert not (tmp_path / "pages.db").exists()

    result = runner.invoke(cli.app, ["import-pages", "--delete-after"])
    assert result.exit_code == 0
    assert not target.exists()
    conn = connect(tmp_path / "pages.db")
    try:
        init_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1
    finally:
        conn.close()
