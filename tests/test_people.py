"""Tests for the team-page people extractor."""
from __future__ import annotations

import asyncio

from leadforge.sources.people import (
    TEAM_PATHS,
    extract_people,
    person_domain_from_wellfound,
    walk_company_people,
)

# ---------------- extract_people ----------------


def test_extract_from_class_based_cards():
    html = """
    <html><body>
      <div class="team-member">
        <h3>Jane Smith</h3>
        <p class="title">Head of Talent</p>
      </div>
      <div class="team-member">
        <h3>Rajesh Kumar</h3>
        <p class="title">Engineering Manager</p>
      </div>
    </body></html>
    """
    people = extract_people(html, "https://acme.test/team", "Acme")
    assert len(people) == 2
    names = [p.name for p in people]
    assert "Jane Smith" in names
    assert "Rajesh Kumar" in names
    jane = next(p for p in people if p.name == "Jane Smith")
    assert jane.title == "Head of Talent"
    assert jane.company == "Acme"
    assert jane.source == "team-page"


def test_extract_rejects_lowercase_names():
    html = """
    <div class="team-member"><h3>not a real name</h3></div>
    """
    assert extract_people(html, "https://acme.test/team", "Acme") == []


def test_extract_rejects_single_word_names():
    html = '<div class="team-member"><h3>Madonna</h3></div>'
    assert extract_people(html, "https://acme.test/team", "Acme") == []


def test_extract_rejects_role_words_as_names():
    html = '<div class="team-member"><h3>Head Engineer</h3></div>'
    # "engineer" is a role keyword, so this is rejected as a name
    assert extract_people(html, "https://acme.test/team", "Acme") == []


def test_extract_structural_fallback():
    """Headings followed by role text should be picked up even without hints."""
    html = """
    <html><body>
      <h3>Priya Sharma</h3>
      <p>Senior Software Engineer</p>
      <h3>Adam Lee</h3>
      <p>Product Manager</p>
    </body></html>
    """
    people = extract_people(html, "https://acme.test/team", "Acme")
    names = [p.name for p in people]
    assert "Priya Sharma" in names
    assert "Adam Lee" in names


def test_extract_dedupes_name_and_title():
    html = """
    <div class="team-member"><h3>Jane Smith</h3><p class="title">CEO</p></div>
    <div class="team-member"><h3>Jane Smith</h3><p class="title">CEO</p></div>
    """
    people = extract_people(html, "https://acme.test/team", "Acme")
    assert len(people) == 1


def test_extract_person_url_from_link():
    html = """
    <div class="team-member">
      <h3>Jane Smith</h3>
      <p class="title">CTO</p>
      <a href="/team/jane-smith">profile</a>
    </div>
    """
    people = extract_people(html, "https://acme.test/team", "Acme")
    assert len(people) == 1
    assert people[0].url == "https://acme.test/team/jane-smith"


def test_extract_empty_html():
    assert extract_people("", "https://acme.test", "Acme") == []


def test_extract_no_candidates():
    html = "<html><body><p>Nothing here</p></body></html>"
    assert extract_people(html, "https://acme.test", "Acme") == []


# ---------------- person_domain_from_wellfound ----------------


def test_person_domain_rejects_wellfound_placeholder():
    assert person_domain_from_wellfound("simplismart.wellfound") == ""


def test_person_domain_passes_real_domain():
    assert person_domain_from_wellfound("simplismart.com") == "simplismart.com"


def test_person_domain_handles_empty():
    assert person_domain_from_wellfound("") == ""


# ---------------- walk_company_people ----------------


class _FakeResponse:
    def __init__(self, html: str):
        self.html = html
        self.status_code = 200
        self.tier = 1
        self.final_url = ""


class _FakeFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str, **kwargs):
        self.calls.append(url)
        if url in self.pages:
            return _FakeResponse(self.pages[url])
        from leadforge.web.errors import FetchError

        raise FetchError(f"not found: {url}")


def test_walk_skips_wellfound_placeholder():
    fake = _FakeFetcher({})
    people = asyncio.run(
        walk_company_people("Simplismart", "simplismart.wellfound", fetcher=fake)
    )
    assert people == []
    assert fake.calls == []


def test_walk_fetches_team_paths_and_extracts():
    fake = _FakeFetcher({
        "https://acme.test/team": """
            <div class="team-member">
              <h3>Jane Smith</h3>
              <p class="title">Engineering Manager</p>
            </div>
        """,
    })
    people = asyncio.run(
        walk_company_people("Acme", "acme.test", fetcher=fake)
    )
    assert len(people) == 1
    assert people[0].name == "Jane Smith"
    assert people[0].title == "Engineering Manager"
    assert "https://acme.test/team" in fake.calls


def test_walk_dedupes_across_paths():
    same_page = """
        <div class="team-member">
          <h3>Jane Smith</h3>
          <p class="title">CTO</p>
        </div>
    """
    fake = _FakeFetcher({
        "https://acme.test/team": same_page,
        "https://acme.test/about": same_page,
    })
    people = asyncio.run(
        walk_company_people("Acme", "acme.test", fetcher=fake)
    )
    assert len(people) == 1


def test_walk_returns_empty_on_all_fetch_failures():
    fake = _FakeFetcher({})
    people = asyncio.run(
        walk_company_people("Acme", "acme.test", fetcher=fake)
    )
    assert people == []


def test_walk_limits_fetches():
    fake = _FakeFetcher({})
    asyncio.run(walk_company_people("Acme", "acme.test", fetcher=fake, max_pages=3))
    assert len(fake.calls) == 3


def test_team_paths_includes_standard_locations():
    for path in ("/team", "/about", "/people"):
        assert path in TEAM_PATHS
# ---------------- regression: real false positives from Augury (2026-10-27) ----------------

def test_reject_our_values():
    html = '<div class="team-member"><h3>Our Values</h3></div>'
    assert extract_people(html, "https://augury.com/about", "Augury") == []


def test_reject_industry_recognition():
    html = '<div class="team-member"><h3>Industry Recognition</h3></div>'
    assert extract_people(html, "https://augury.com/about", "Augury") == []


def test_reject_our_investors():
    html = '<div class="team-member"><h3>Our Investors</h3></div>'
    assert extract_people(html, "https://augury.com/about", "Augury") == []


def test_reject_drop_us_a_line():
    html = '<div class="team-member"><h3>Drop Us a Line</h3></div>'
    assert extract_people(html, "https://augury.com/about", "Augury") == []


# ---------------- title is required ----------------

def test_reject_name_without_title():
    """A name alone (no title found) is not enough — likely nav."""
    html = '<div class="team-member"><h3>Jane Smith</h3></div>'
    assert extract_people(html, "https://acme.test/team", "Acme") == []


def test_reject_title_without_role_keyword():
    html = """
    <div class="team-member">
      <h3>Jane Smith</h3>
      <p class="title">Some Random Text</p>
    </div>
    """
    assert extract_people(html, "https://acme.test/team", "Acme") == []


def test_reject_paragraph_as_title():
    """Multi-sentence prose is not a valid title."""
    prose = (
        "Jane leads our team. She brings 10 years of experience. "
        "Her focus is on scaling."
    )
    html = f"""
    <div class="team-member">
      <h3>Jane Smith</h3>
      <p class="title">{prose}</p>
    </div>
    """
    assert extract_people(html, "https://acme.test/team", "Acme") == []


def test_accept_name_with_real_title():
    """The happy path still works: real name + real title."""
    html = """
    <div class="team-member">
      <h3>Jane Smith</h3>
      <p class="title">Head of Talent</p>
    </div>
    """
    people = extract_people(html, "https://acme.test/team", "Acme")
    assert len(people) == 1
    assert people[0].name == "Jane Smith"
    assert people[0].title == "Head of Talent"


def test_accept_engineering_title():
    html = """
    <div class="team-member">
      <h3>Rajesh Kumar</h3>
      <p class="title">Senior Software Engineer</p>
    </div>
    """
    people = extract_people(html, "https://acme.test/team", "Acme")
    assert len(people) == 1


def test_accept_recruiter_title():
    html = """
    <div class="team-member">
      <h3>Priya Sharma</h3>
      <p class="title">Technical Recruiter</p>
    </div>
    """
    people = extract_people(html, "https://acme.test/team", "Acme")
    assert len(people) == 1