"""Cross-check model claims against deterministic page extraction results."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from leadforge.llm.schemas import NextLink, PageExtraction
from leadforge.web.denylist import check_host
from leadforge.web.errors import DeniedHost
from leadforge.web.extract import FoundEmail, Link

PERSONAL_SOCIAL_HOSTS = {
    "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com",
    "snapchat.com", "reddit.com",
}


def validate_page_extraction(
    extraction: PageExtraction,
    *,
    cleaned_text: str,
    extracted_emails: list[FoundEmail] | set[str],
    candidate_links: list[Link] | list[str],
    denylist_hosts: set[str] | None = None,
    page_host: str = "",
) -> PageExtraction:
    allowed_emails = {
        item.email.lower() if isinstance(item, FoundEmail) else item.lower()
        for item in extracted_emails
    }
    urls = [item.url if isinstance(item, Link) else item for item in candidate_links]
    url_to_id = {url: index for index, url in enumerate(urls, start=1)}
    id_to_url = {index: url for url, index in url_to_id.items()}
    denied = denylist_hosts or set()
    social_page = _host_matches(page_host, PERSONAL_SOCIAL_HOSTS) or _host_matches(
        page_host, denied
    )
    text_tokens = _tokens(cleaned_text)

    people = []
    for person in extraction.people:
        name_tokens = _tokens(person.name)
        if not name_tokens or not name_tokens.issubset(text_tokens):
            continue
        emails = [email for email in person.emails_published if email.lower() in allowed_emails]
        profile_links = [
            link for link in person.profile_links
            if link in url_to_id and not _is_denied(link, denied)
        ]
        person_values = person.model_dump()
        person_values.update(
            emails_published=emails,
            profile_links=profile_links,
            professional=False if social_page else person.professional,
        )
        people.append(type(person).model_validate(person_values))

    next_links: list[NextLink] = []
    seen: set[str] = set()
    for item in extraction.next_links:
        url = id_to_url.get(item.candidate_id) if item.candidate_id is not None else item.url
        if not url or url not in url_to_id or url in seen or _is_denied(url, denied):
            continue
        seen.add(url)
        link_values = item.model_dump()
        link_values.update(url=url, candidate_id=url_to_id[url])
        next_links.append(NextLink.model_validate(link_values))
        if len(next_links) == 5:
            break
    values = extraction.model_dump()
    values.update(
        people=people,
        org_emails=[
            email for email in extraction.org_emails if email.lower() in allowed_emails
        ],
        next_links=next_links,
    )
    return PageExtraction.model_validate(values)


def _tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", text).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return set(re.findall(r"[a-z0-9]+", normalized))


def _host_matches(host: str, domains: set[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _is_denied(url: str, denied: set[str]) -> bool:
    host = urlsplit(url).hostname
    if not host:
        return True
    try:
        check_host(host, denied)
    except DeniedHost:
        return True
    return False
