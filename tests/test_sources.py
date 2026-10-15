import re
from pathlib import Path

from leadforge.models import Job
from leadforge.sources.ats import (
    detect_ats_link,
    parse_ashby,
    parse_greenhouse,
    parse_lever,
)
from leadforge.sources.careers import is_js_shell, parse_careers_page
from leadforge.sources.hn import hiring_comments, parse_hiring_comment
from leadforge.stages.matching import match_job
from leadforge.stages.profile import extract_profile

FIXTURES = Path(__file__).parent / "fixtures" / "sources" / "phase13"


def test_ats_parsers_match_live_response_fixtures() -> None:
    greenhouse = parse_greenhouse(
        (FIXTURES / "greenhouse.json").read_text(encoding="utf-8"), "Stripe", "stripe.com"
    )
    lever = parse_lever(
        (FIXTURES / "lever.json").read_text(encoding="utf-8"), "Example", "example.org"
    )
    ashby = parse_ashby(
        (FIXTURES / "ashby.json").read_text(encoding="utf-8"), "Linear", "linear.app"
    )

    assert greenhouse and all(job.source == "greenhouse" for job in greenhouse)
    assert lever and all(job.source == "lever" for job in lever)
    assert ashby and all(job.source == "ashby" for job in ashby)
    assert all(job.url.startswith("https://") for job in greenhouse + lever + ashby)
    fixture_text = " ".join(path.read_text(encoding="utf-8") for path in FIXTURES.glob("*.json"))
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", fixture_text)


def test_ats_link_detection_only_accepts_known_hosts() -> None:
    assert detect_ats_link(["https://jobs.lever.co/example/123"]) == ("lever", "example")
    assert detect_ats_link(["https://jobs.ashbyhq.com/example"]) == ("ashby", "example")
    assert detect_ats_link(["https://malicious.test/greenhouse/example"]) is None


def test_profile_and_job_matching_are_deterministic() -> None:
    profile = extract_profile("Python SQL software engineer AWS")
    job = Job(
        company="Acme",
        domain="acme.example",
        title="Junior Python Engineer",
        url="https://jobs.example.org/1",
        description="Build APIs using Python and SQL",
        source="fixture",
    )
    matched = match_job(job, profile)
    assert "skill:python" in matched.match_reasons
    assert matched.match_score > 0
    assert profile.skills == ["aws", "python", "sql"]


def test_hn_who_is_hiring_comment_extracts_professional_email_signal() -> None:
    job = parse_hiring_comment(
        {
            "id": 123,
            "text": (
                "Acme | Junior Data Engineer | Remote. Please email "
                "jobs@example.org with your resume."
            ),
        }
    )
    assert job is not None
    assert job.company == "Acme"
    assert job.title == "Junior Data Engineer"
    assert job.invites_email
    assert "jobs@example.org" in job.description


async def test_hn_comment_fetch_uses_only_top_level_story_kids() -> None:
    requested = []

    async def fetch_json(url: str):
        requested.append(url)
        return {"id": 12, "text": "Acme | Engineer | email jobs@example.org with resume"}

    items = await hiring_comments(fetch_json, {"kids": [12, 13]}, max_comments=1)

    assert len(items) == 1
    assert requested == ["https://hacker-news.firebaseio.com/v0/item/12.json"]


def test_careers_heading_extraction_and_js_shell_signal() -> None:
    page = '<h2><a href="/jobs/1">Software Engineer</a></h2>'
    jobs = parse_careers_page(page, "Acme", "acme.test", "https://acme.test/careers")
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].url == "https://acme.test/jobs/1"
    assert is_js_shell("<div id='root'></div><script></script>")
    assert (
        parse_careers_page(
            "<div id='root'></div><script></script>", "A", "a.test", "https://a.test"
        )
        == []
    )
