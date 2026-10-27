"""Extract people (name + title) from company team pages.

Deterministic, offline-testable. No search engine calls, no LinkedIn scraping.
Two entry points:

  extract_people(html, source_url, company) -> list[PersonCandidate]
      Pure parser. Useful in tests and replay.

  walk_company_people(company, domain, *, fetcher=None) -> list[PersonCandidate]
      Fetches a small set of standard team/about paths through the policy
      router and concatenates the results. Deduplicates by (name, title).
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from leadforge.models import PersonCandidate

TEAM_PATHS: tuple[str, ...] = (
    "/team",
    "/about",
    "/about-us",
    "/people",
    "/company",
    "/company/team",
    "/leadership",
    "/contact",
)

# DOM class/id substrings that strongly indicate a person card.
_PERSON_CONTAINER_HINTS: tuple[str, ...] = (
    "team-member", "team_member", "teammember",
    "person-card", "person_card", "personcard",
    "member-card", "member_card",
    "profile-card", "profile_card",
    "staff-member", "staff_member",
    "bio",
)

_TITLE_HINTS: tuple[str, ...] = (
    "title", "role", "position", "job-title", "job_title",
    "designation", "rank",
)

# Titles that are unlikely to be names even if they look like one.
_NON_NAME_WORDS: frozenset[str] = frozenset({
    "team", "about", "contact", "leadership", "people", "careers",
    "home", "menu", "search", "login", "signup", "sign", "log",
    "privacy", "terms", "cookies", "policy",
    "founder", "engineer", "manager", "director", "head", "lead",
    "ceo", "cto", "coo", "cfo", "co-founder", "cofounder",
})

# Title keywords that usually follow a person name.
_ROLE_KEYWORDS: tuple[str, ...] = (
    "engineer", "engineering", "developer", "designer", "scientist",
    "manager", "director", "lead", "head", "founder", "co-founder",
    "cto", "ceo", "coo", "cfo", "vp", "vice president", "president",
    "analyst", "consultant", "architect", "specialist", "recruiter",
    "talent", "hr", "people ops", "people partner", "officer",
    "intern", "associate", "principal", "staff", "chief",
)

_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'\-\. ]{1,60}$")


def _is_probable_name(text: str) -> bool:
    """Two to four capitalized words, no obvious role words, reasonable length."""
    if not text:
        return False
    cleaned = " ".join(text.split())
    if not _NAME_RE.match(cleaned):
        return False
    words = cleaned.split()
    if not (2 <= len(words) <= 4):
        return False
    low = cleaned.lower()
    if any(w in low for w in _NON_NAME_WORDS):
        return False
    if any(k in low for k in _ROLE_KEYWORDS):
        return False
    if len(cleaned) > 60:
        return False
    return True


def _has_role_keyword(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(k in low for k in _ROLE_KEYWORDS)


def _class_id_contains(el: Tag, hints: tuple[str, ...]) -> bool:
    """True if any hint appears in el's class or id attribute."""
    for attr in ("class", "id"):
        val = el.get(attr)
        if not val:
            continue
        if isinstance(val, list):
            joined = " ".join(val).lower()
        else:
            joined = str(val).lower()
        for hint in hints:
            if hint in joined:
                return True
    return False


def _extract_title(el: Tag) -> str:
    """Best-effort title extraction from within a person container."""
    # Try explicit title classes first
    for child in el.find_all(True):
        if not isinstance(child, Tag):
            continue
        if _class_id_contains(child, _TITLE_HINTS):
            text = " ".join(child.get_text(" ", strip=True).split())
            if text and len(text) < 80:
                return text
    # Fallback: any <p> or <span> that contains a role keyword
    for child in el.find_all(["p", "span", "div"]):
        if not isinstance(child, Tag):
            continue
        text = " ".join(child.get_text(" ", strip=True).split())
        if text and len(text) < 80 and _has_role_keyword(text):
            return text
    return ""


def _extract_name(el: Tag) -> str:
    """Best-effort name extraction from within a person container."""
    # Prefer headings
    for tag in ("h1", "h2", "h3", "h4", "h5", "strong", "b"):
        node = el.find(tag)
        if node is not None:
            text = " ".join(node.get_text(" ", strip=True).split())
            if _is_probable_name(text):
                return text
    # Fallback: any short text node that looks like a name
    for child in el.find_all(True):
        if not isinstance(child, Tag):
            continue
        text = " ".join(child.get_text(" ", strip=True).split())
        if _is_probable_name(text):
            return text
    return ""


def _person_url(el: Tag, base_url: str) -> str:
    """Find a profile link within the container, resolved against base_url."""
    for a in el.find_all("a", href=True):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        low = href.lower()
        if low.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base_url, href)
        # Prefer links that look personal (contain a name-slug)
        if any(seg in low for seg in ("/team", "/people", "/bio", "/staff")):
            return full
    return ""


def _extract_from_container(
    el: Tag, company: str, source_url: str
) -> PersonCandidate | None:
    name = _extract_name(el)
    if not name:
        return None
    title = _extract_title(el)
    url = _person_url(el, source_url) or source_url
    return PersonCandidate(
        name=name,
        title=title,
        company=company,
        url=url,
        snippet="",
        source="team-page",
    )


def extract_people(
    html: str,
    source_url: str,
    company: str,
) -> list[PersonCandidate]:
    """Extract people from a single team/about HTML page."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")

    people: list[PersonCandidate] = []
    seen: set[tuple[str, str]] = set()

    # Pass 1: containers that look like person cards.
    for el in soup.find_all(True):
        if not isinstance(el, Tag):
            continue
        if not _class_id_contains(el, _PERSON_CONTAINER_HINTS):
            continue
        candidate = _extract_from_container(el, company, source_url)
        if candidate is None:
            continue
        key = (candidate.name.lower(), candidate.title.lower())
        if key in seen:
            continue
        seen.add(key)
        people.append(candidate)

    # Pass 2 (structural fallback): headings immediately followed by role text.
    for heading in soup.find_all(["h2", "h3", "h4"]):
        if not isinstance(heading, Tag):
            continue
        name = " ".join(heading.get_text(" ", strip=True).split())
        if not _is_probable_name(name):
            continue
        # Look at next sibling for role text
        sibling = heading.find_next_sibling()
        title = ""
        hops = 0
        while sibling is not None and hops < 3:
            if isinstance(sibling, Tag):
                text = " ".join(sibling.get_text(" ", strip=True).split())
                if text and _has_role_keyword(text) and len(text) < 80:
                    title = text
                    break
            sibling = sibling.find_next_sibling()
            hops += 1
        key = (name.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        people.append(
            PersonCandidate(
                name=name,
                title=title,
                company=company,
                url=source_url,
                snippet="",
                source="team-page",
            )
        )

    # Cap per-page extraction. Real team pages have <100 people; if we
    # exceeded that, the page is probably a directory or has a scraper-hostile
    # layout. Keeping the first N (in DOM order) preserves the "senior people
    # are usually first" pattern of most team pages.
    return people[:40]


async def walk_company_people(
    company: str,
    domain: str,
    *,
    fetcher=None,
    max_pages: int = 5,
) -> list[PersonCandidate]:
    """Fetch standard team/about paths and concatenate extracted people.

    Returns at most max_pages worth of fetches. Skips fake `.wellfound`
    placeholder domains (Wellfound does not publish real startup websites).
    Returns [] on any failure; does not raise.
    """
    if not domain or domain.endswith(".wellfound"):
        return []

    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    base = f"https://{domain}"
    collected: list[PersonCandidate] = []
    seen: set[tuple[str, str]] = set()

    for path in TEAM_PATHS[:max_pages]:
        url = base + path
        try:
            resp = await router.fetch(url, purpose="person")
        except FetchError:
            continue
        page_people = extract_people(resp.html or "", url, company)
        # Over-count guard: if a single page yields >60 people, it's probably
        # a directory/aggregator page, not a team page. Skip it.
        if len(page_people) > 60:
            continue
        for p in page_people:
            key = (p.name.lower(), p.title.lower())
            if key in seen:
                continue
            seen.add(key)
            collected.append(p)

    # Cap total across pages
    return collected[:60]


def person_domain_from_wellfound(domain: str) -> str:
    """Return '' for .wellfound placeholders, else the domain unchanged.

    Team-page walking needs a real hostname. Wellfound placeholder domains
    look real but resolve nowhere, so we refuse them explicitly to avoid
    pointless network calls and false negatives.
    """
    if not domain:
        return ""
    if domain.endswith(".wellfound"):
        return ""
    return domain