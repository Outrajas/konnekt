"""Extract people (name + title) from company team pages.

Deterministic, offline-testable. No search engine calls, no LinkedIn scraping.

Two entry points:
  extract_people(html, source_url, company) -> list[PersonCandidate]
  walk_company_people(company, domain, *, fetcher=None) -> list[PersonCandidate]

Extraction rule (since 2026-10-27): a container counts as a person ONLY if
1. name is 2-4 Title-Case words, no nav/product words
2. AND a title is found nearby that contains a role keyword
   (engineer/manager/talent/recruiter/head/etc.)
3. AND that title is a short phrase (<=80 chars), not a paragraph

Without the title requirement, "Our Values" and "Drop Us a Line" pass the
name check on nav-heavy pages.
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

_ROLE_KEYWORDS: tuple[str, ...] = (
    "engineer", "engineering", "developer", "designer", "scientist",
    "manager", "director", "lead", "head", "founder", "co-founder",
    "cto", "ceo", "coo", "cfo", "vp", "vice president", "president",
    "analyst", "consultant", "architect", "specialist", "recruiter",
    "talent", "hr", "people ops", "people partner", "officer",
    "intern", "associate", "principal", "staff", "chief", "coordinator",
    "administrator", "executive", "assistant",
)

# Name connectors that may be lowercase in real names (Maria de la Cruz).
_NAME_CONNECTORS: frozenset[str] = frozenset({
    "de", "da", "do", "dos", "das", "van", "von", "der", "den",
    "le", "la", "du", "des", "al", "bin", "binte", "ibn",
})

# Words that, if present in a name candidate, disqualify it.
# Curated: only words that are NEVER valid English person names.
_STOP_NAME_WORDS: frozenset[str] = frozenset({
    # Pronouns / articles / question words
    "our", "your", "their", "its", "we", "us", "them", "they", "you",
    "what", "why", "how", "when", "where", "who", "which",
    "the", "this", "that", "these", "those",
    # Conjunctions and prepositions
    "and", "or", "but", "for", "with", "from", "into", "over", "under",
    "between", "about",
    # Navigation
    "home", "team", "contact", "blog", "news", "press",
    "careers", "jobs", "positions", "openings", "opportunities",
    "product", "products", "pricing", "plans", "features", "feature",
    "solutions", "solution", "resources", "docs", "documentation",
    "api", "sdk", "support", "help", "faq", "login", "signup",
    "register", "search", "menu", "sitemap", "privacy", "terms",
    "cookies", "policy", "legal",
    # Content sections
    "values", "mission", "vision", "story", "history", "culture",
    "investors", "recognition", "industry", "awards", "testimonials",
    "reviews", "clients", "partners", "customers", "case",
    "study", "stories", "insights", "research", "technology",
    "platform", "services", "service", "business", "enterprise",
    "startups", "startup",
    # Actions
    "drop", "reach", "connect", "get", "started", "learn", "read",
    "see", "view", "click", "submit", "send", "join", "try",
    "buy", "sell", "build", "make", "create", "develop",
    # Products (belt-and-suspenders)
    "code", "science", "slides", "artifacts", "claude", "gpt",
    "chatgpt", "gemini", "copilot", "pro", "premium", "plus",
    "free", "basic", "standard",
    # Filler
    "information", "details", "additional", "more", "less", "all",
    "new", "old", "latest", "next", "previous", "today", "now",
    "here", "there", "nowhere", "somewhere",
    # Image alt text
    "logo", "image", "icon", "avatar", "photo", "banner", "hero",
    "cover", "picture", "illustration", "thumbnail", "screenshot",
    # Roles (belt-and-suspenders)
    "engineer", "engineering", "developer", "designer", "scientist",
    "manager", "director", "lead", "head", "founder", "ceo", "cto",
    "coo", "cfo", "vp", "president", "chief", "officer", "architect",
    "consultant", "analyst", "intern", "associate", "principal",
    "staff", "recruiter", "talent", "hr",
})


def _is_probable_name(text: str) -> bool:
    """Strict Title-Case name validation.

    Accepts:  Jane Smith, Priya Sharma, Mary Jane Watson, Maria de la Cruz
    Rejects:  Our Values, Drop Us a Line, Industry Recognition,
              Compare features across plans, Claude Code, Head Engineer
    """
    if not text:
        return False
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > 60:
        return False
    words = cleaned.split()
    if not (2 <= len(words) <= 4):
        return False
    title_count = 0
    for w in words:
        if not w:
            return False
        low = w.lower()
        if low in _NAME_CONNECTORS:
            continue
        if low in _STOP_NAME_WORDS:
            return False
        # Title Case: first char uppercase, rest lowercase; OR acronym
        is_title = w[0].isupper() and (len(w) == 1 or w[1:].islower() or w.isupper())
        if not is_title:
            return False
        title_count += 1
    return title_count >= 2


def _looks_like_paragraph(text: str) -> bool:
    """Reject prose masquerading as a title (multiple sentences, very long)."""
    if not text:
        return True
    if len(text) > 120:
        return True
    # Two or more sentences (period/question mark/exclamation + space + capital)
    sentences = re.findall(r"[.!?]\s+[A-Z]", text)
    if len(sentences) >= 1:
        return True
    # Comma-heavy prose with no role keyword has already been excluded by caller;
    # this is a pure "is this a sentence" heuristic.
    return False


def _has_role_keyword(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(k in low for k in _ROLE_KEYWORDS)


def _class_id_contains(el: Tag, hints: tuple[str, ...]) -> bool:
    for attr in ("class", "id"):
        val = el.get(attr)
        if not val:
            continue
        joined = " ".join(val).lower() if isinstance(val, list) else str(val).lower()
        for hint in hints:
            if hint in joined:
                return True
    return False


def _extract_title(el: Tag) -> str:
    """Best-effort title extraction; rejects paragraphs."""
    for child in el.find_all(True):
        if not isinstance(child, Tag):
            continue
        if _class_id_contains(child, _TITLE_HINTS):
            text = " ".join(child.get_text(" ", strip=True).split())
            if text and len(text) < 80 and not _looks_like_paragraph(text):
                return text
    for child in el.find_all(["p", "span", "div"]):
        if not isinstance(child, Tag):
            continue
        text = " ".join(child.get_text(" ", strip=True).split())
        if (
            text
            and len(text) < 80
            and _has_role_keyword(text)
            and not _looks_like_paragraph(text)
        ):
            return text
    return ""


def _extract_name(el: Tag) -> str:
    for tag in ("h1", "h2", "h3", "h4", "h5", "strong", "b"):
        node = el.find(tag)
        if node is not None:
            text = " ".join(node.get_text(" ", strip=True).split())
            if _is_probable_name(text):
                return text
    for child in el.find_all(True):
        if not isinstance(child, Tag):
            continue
        text = " ".join(child.get_text(" ", strip=True).split())
        if _is_probable_name(text):
            return text
    return ""


def _person_url(el: Tag, base_url: str) -> str:
    for a in el.find_all("a", href=True):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        low = href.lower()
        if low.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base_url, href)
        if any(seg in low for seg in ("/team", "/people", "/bio", "/staff", "/person")):
            return full
    return ""


def _extract_from_container(
    el: Tag, company: str, source_url: str
) -> PersonCandidate | None:
    name = _extract_name(el)
    if not name:
        return None
    title = _extract_title(el)
    # REQUIRE a title. Without one, this is likely a nav heading or list item.
    if not title:
        return None
    # REQUIRE the title to contain a role keyword. Real team cards always do.
    if not _has_role_keyword(title):
        return None
    url = _person_url(el, source_url) or source_url
    return PersonCandidate(
        name=name, title=title, company=company,
        url=url, snippet="", source="team-page",
    )


def extract_people(
    html: str,
    source_url: str,
    company: str,
) -> list[PersonCandidate]:
    """Extract people from a team/about HTML page."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")

    people: list[PersonCandidate] = []
    seen: set[tuple[str, str]] = set()

    # Pass 1: containers matching person class/id hints.
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

    # Pass 2: img[alt] + adjacent role text (standard team-card pattern).
    for img in soup.find_all("img", alt=True):
        if not isinstance(img, Tag):
            continue
        alt_raw = img.get("alt")
        alt = " ".join((str(alt_raw) if alt_raw else "").split())
        if not _is_probable_name(alt):
            continue
        container = img.find_parent(["div", "li", "article", "section"])
        if container is None:
            continue
        title = _extract_title(container)
        # Same requirement: real title with role keyword.
        if not title or not _has_role_keyword(title):
            continue
        url = _person_url(container, source_url) or source_url
        key = (alt.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        people.append(
            PersonCandidate(
                name=alt, title=title, company=company,
                url=url, snippet="", source="team-page",
            )
        )

    # Pass 3: heading followed by SHORT role text (must contain role keyword).
    for heading in soup.find_all(["h2", "h3", "h4"]):
        if not isinstance(heading, Tag):
            continue
        name = " ".join(heading.get_text(" ", strip=True).split())
        if not _is_probable_name(name):
            continue
        title = ""
        sibling = heading.find_next_sibling()
        for _ in range(3):
            if sibling is None:
                break
            if isinstance(sibling, Tag):
                text = " ".join(sibling.get_text(" ", strip=True).split())
                if (
                    text
                    and len(text) <= 80
                    and _has_role_keyword(text)
                    and not _looks_like_paragraph(text)
                ):
                    title = text
                    break
            sibling = sibling.find_next_sibling()
        if not title:
            continue
        key = (name.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        people.append(
            PersonCandidate(
                name=name, title=title, company=company,
                url=source_url, snippet="", source="team-page",
            )
        )

    return people[:40]


async def walk_company_people(
    company: str,
    domain: str,
    *,
    fetcher=None,
    max_pages: int = 5,
) -> list[PersonCandidate]:
    """Fetch standard team/about paths and concatenate extracted people."""
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
        # Over-count guard: a single page yielding >60 people is probably a
        # directory page, not a team page.
        if len(page_people) > 60:
            continue
        for p in page_people:
            key = (p.name.lower(), p.title.lower())
            if key in seen:
                continue
            seen.add(key)
            collected.append(p)

    return collected[:60]


def person_domain_from_wellfound(domain: str) -> str:
    """Return '' for .wellfound placeholders, else domain unchanged."""
    if not domain:
        return ""
    if domain.endswith(".wellfound"):
        return ""
    return domain