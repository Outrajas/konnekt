"""Hacker News Who-is-Hiring helpers: legacy comment parsing plus board adapter.

Two generations of API live here on purpose:

* ``parse_hiring_comment`` / ``hiring_comments`` / ``discover_hiring_posts`` — the
  original Firebase-based pair used by ``stages.discovery`` and the source tests.
  The fetch function is injected so callers keep every request on the policy
  router.
* ``parse_hn_comments`` / ``fetch_hn_jobs`` — the board-dispatcher adapter added
  with the 14-board expansion, which reads the Algolia mirror instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from html import unescape

from bs4 import BeautifulSoup

from leadforge.models import Job
from leadforge.seniority import infer_seniority
from leadforge.web.errors import FetchError
from leadforge.web.fetcher import Fetcher

FetchJsonFn = Callable[[str], Awaitable[dict | list | None]]
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
ALGOLIA_COMMENTS = (
    "https://hn.algolia.com/api/v1/search?query=who+is+hiring&tags=comment&hitsPerPage=20"
)
ALGOLIA_STORIES = "https://hn.algolia.com/api/v1/search?query=who+is+hiring&tags=story&hitsPerPage=5"


def parse_hiring_comment(item: dict, company_hint: str = "") -> Job | None:
    """Convert a Who is Hiring comment to a company/job signal when identifiable."""
    raw = item.get("text")
    if not isinstance(raw, str):
        return None
    soup = BeautifulSoup(unescape(raw), "html.parser")
    text = " ".join(soup.get_text(" ").split())
    first_line = re.split(r"\s*(?:\||—|–)\s*", text, maxsplit=2)
    if len(first_line) < 2:
        return None
    company = first_line[0].strip(" -*") or company_hint
    title = first_line[1].strip(" -*")
    if not company or not title:
        return None
    invite = bool(re.search(r"\b(email|e-mail)\b.{0,100}\b(resume|résumé|cv)\b", text, re.I))
    link = item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id', '')}"
    return Job(
        company=company,
        domain="",
        title=title,
        url=str(link),
        description=text,
        source="hacker-news",
        invites_email=invite,
        seniority=infer_seniority(title, text),
    )


async def hiring_comments(
    fetch_json: FetchJsonFn, story: dict, *, max_comments: int = 100
) -> list[dict]:
    """Read top-level HN comments with an injected router-backed JSON fetcher."""
    kids = story.get("kids", [])
    if not isinstance(kids, list):
        return []
    result: list[dict] = []
    for item_id in kids[:max_comments]:
        item = await fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            result.append(item)
    return result


async def discover_hiring_posts(
    *, fetcher: Fetcher | None = None, max_stories: int = 200, max_comments: int = 100
) -> list[Job]:
    """Find the latest Who is Hiring thread and parse its top-level comments."""
    router = fetcher or Fetcher()

    async def get_json(url: str):
        response = await router.fetch(url, min_tier=1, max_tier=1)
        try:
            return json.loads(response.html)
        except json.JSONDecodeError:
            return None

    user = await get_json("https://hacker-news.firebaseio.com/v0/user/whoishiring.json")
    story_ids = user.get("submitted", []) if isinstance(user, dict) else []
    if not isinstance(story_ids, list):
        return []
    story = None
    for item_id in story_ids[:max_stories]:
        item = await get_json(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
        if isinstance(item, dict) and re.search(
            r"who is hiring", str(item.get("title", "")), re.IGNORECASE
        ):
            story = item
            break
    if story is None:
        return []
    comments = await hiring_comments(get_json, story, max_comments=max_comments)
    return [job for item in comments if (job := parse_hiring_comment(item)) is not None]


# ------------------------------------------------------------------ board adapter
def parse_hn_comments(raw: str | dict, *, profile=None) -> list[Job]:
    """Parse Algolia comment hits into jobs (14-board dispatcher adapter)."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        data = raw

    hits = data.get("hits", []) if isinstance(data, dict) else []
    jobs: list[Job] = []
    for hit in hits:
        comment_text = hit.get("comment_text") or ""
        if "engineer" not in comment_text.lower() and "developer" not in comment_text.lower():
            continue
        url = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        title = "HN: " + (comment_text[:80].replace("\n", " ") + "...")
        desc = comment_text[:8000]
        jobs.append(
            Job(
                company="HN Post",
                domain="hacker-news.test",
                title=title,
                url=url,
                location="Various",
                description=desc,
                source="hn-who-is-hiring",
                remote=False,
                seniority=infer_seniority(title, desc),
            )
        )
    return jobs


async def fetch_hn_jobs(profile, *, fetcher=None) -> list[Job]:
    """Fetch the Algolia Who-is-Hiring comments through the policy router."""
    router = fetcher or Fetcher()
    try:
        response = await router.fetch(ALGOLIA_COMMENTS, min_tier=1, max_tier=1, purpose="job")
    except FetchError:
        return []
    return parse_hn_comments(response.html or "", profile=profile)
