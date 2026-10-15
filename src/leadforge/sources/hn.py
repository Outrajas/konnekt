"""Hacker News Firebase parsing helpers; callers inject policy-routed fetches."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from html import unescape

from bs4 import BeautifulSoup

from leadforge.models import Job
from leadforge.web.fetcher import Fetcher

FetchJsonFn = Callable[[str], Awaitable[dict | list | None]]
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


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
