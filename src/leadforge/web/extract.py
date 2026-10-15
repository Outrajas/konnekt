"""Deterministic extraction helpers. This module never performs network I/O."""

import re
import tomllib
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import tldextract
import trafilatura
from bs4 import BeautifulSoup

from leadforge.web.denylist import check_host, load_denylist
from leadforge.web.errors import DeniedHost

HostKind = Literal["personal_site", "github", "gitlab", "talk", "mail_archive", "company", "other"]
_SUFFIXES = tldextract.TLDExtract(suffix_list_urls=())
_DEFAULT_HOST_KINDS = Path("data/host_kinds.toml")
_DEFAULT_DENYLIST = Path("data/denylist_hosts.txt")
_EMAIL_RE = re.compile(
    r"(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"([A-Z0-9.!#$%&'*+/=?^_`{|}~-]{1,64})@"
    r"((?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63})"
    r"(?![A-Z0-9-])",
    re.IGNORECASE,
)
_OBFUSCATED_AT_DOT = re.compile(
    r"(?P<local>[A-Z0-9._%+-]{1,64})\s*(?:\[at\]|\(at\)|\bat\b)\s*"
    r"(?P<domain>[A-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\bdot\b)\s*"
    r"(?P<tld>[A-Z]{2,63})(?![A-Z0-9@.-])",
    re.IGNORECASE,
)
_OBFUSCATED_AT = re.compile(
    r"(?P<local>[A-Z0-9._%+-]{1,64})\s*(?:\[at\]|\(at\)|\bat\b)\s*"
    r"(?P<domain>(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63})"
    r"(?![A-Z0-9@.-])",
    re.IGNORECASE,
)
_CONTACT_CONTEXT = re.compile(r"\b(contact|email|e-mail|reach|write|mail)\b", re.IGNORECASE)
_TRACKING_LOCAL = re.compile(r"(?=.*[0-9])[a-f0-9]{6,}", re.IGNORECASE)
_IGNORED_LOCAL = {"noreply", "no-reply", "no_reply", "donotreply", "do-not-reply"}


@dataclass(frozen=True)
class Link:
    url: str
    anchor_text: str
    rel: tuple[str, ...]
    same_domain: bool
    in_navigation: bool


@dataclass(frozen=True)
class FoundEmail:
    email: str
    raw_match: str


def clean_text(html: str, url: str = "", max_chars: int = 12_000) -> str:
    """Extract readable page text and keep whole paragraphs within the character budget."""
    extracted = trafilatura.extract(
        html, url=url or None, favor_recall=True, include_links=False, include_comments=False
    )
    if not extracted:
        soup = BeautifulSoup(html, "lxml")
        for node in soup(("script", "style", "noscript", "svg")):
            node.decompose()
        extracted = soup.get_text("\n\n")
    paragraphs = [" ".join(unescape(p).split()) for p in extracted.splitlines()]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    result: list[str] = []
    used = 0
    for paragraph in paragraphs:
        added = len(paragraph) + (2 if result else 0)
        if used + added > max_chars:
            break
        result.append(paragraph)
        used += added
    return "\n\n".join(result)


def registrable_domain(host: str) -> str:
    """Return a registrable domain using only tldextract's bundled suffix snapshot."""
    host = host.rstrip(".").lower()
    extracted = _SUFFIXES(host)
    return extracted.top_domain_under_public_suffix or host


def extract_links(
    html: str,
    base_url: str,
    *,
    denylist: set[str] | None = None,
) -> list[Link]:
    """Extract normalized HTTP(S) links and retain navigation and same-site metadata."""
    denied = load_denylist(_DEFAULT_DENYLIST) if denylist is None else denylist
    base = urlsplit(base_url)
    base_domain = registrable_domain(base.hostname or "")
    soup = BeautifulSoup(html, "lxml")
    links: list[Link] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = unescape(str(anchor.get("href", "")).strip())
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            continue
        clean_url = urlunsplit(
            (parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, "")
        )
        if clean_url in seen:
            continue
        try:
            check_host(parts.hostname, denied)
        except DeniedHost:
            continue
        seen.add(clean_url)
        rel_value = anchor.get("rel", [])
        rel = tuple(sorted(str(value).lower() for value in rel_value))
        links.append(
            Link(
                url=clean_url,
                anchor_text=" ".join(anchor.get_text(" ", strip=True).split()),
                rel=rel,
                same_domain=registrable_domain(parts.hostname) == base_domain,
                in_navigation=anchor.find_parent(("nav", "footer")) is not None,
            )
        )
    return links


def extract_emails(html_and_text: str) -> list[FoundEmail]:
    """Find published addresses and deliberately obfuscated addresses with contact context."""
    source = unescape(html_and_text)
    soup = BeautifulSoup(source, "lxml")
    found: dict[str, FoundEmail] = {}

    def add(
        address: str,
        raw: str,
        *,
        obfuscated: bool = False,
        start: int = 0,
        context_source: str | None = None,
    ) -> None:
        normalized = address.strip().lower().rstrip(".,;:!?)]")
        if not _is_usable_email(normalized):
            return
        if obfuscated:
            context_text = context_source or source
            context = context_text[max(0, start - 100):start + len(raw) + 100]
            if not _CONTACT_CONTEXT.search(BeautifulSoup(context, "lxml").get_text(" ")):
                return
        found.setdefault(normalized, FoundEmail(normalized, raw))

    for match in _EMAIL_RE.finditer(source):
        add(match.group(0), match.group(0), start=match.start())
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if not href.lower().startswith("mailto:"):
            continue
        mailbox = unquote(href[7:].split("?", 1)[0]).strip()
        for address_match in _EMAIL_RE.finditer(mailbox):
            add(address_match.group(0), href, start=0)
    visible_text = BeautifulSoup(source, "lxml").get_text(" ")
    for pattern in (_OBFUSCATED_AT_DOT, _OBFUSCATED_AT):
        for match in pattern.finditer(visible_text):
            domain = match.groupdict().get("domain", "")
            tld = match.groupdict().get("tld")
            if tld:
                domain = f"{domain.rstrip('.')} . {tld}".replace(" ", "")
            add(
                f"{match.group('local')}@{domain}", match.group(0), obfuscated=True,
                start=match.start(), context_source=visible_text,
            )
    return list(found.values())


def _is_usable_email(address: str) -> bool:
    match = _EMAIL_RE.fullmatch(address)
    if not match:
        return False
    local, domain = match.groups()
    local = local.lower().split("+", 1)[0]
    domain = domain.lower().rstrip(".")
    if local in _IGNORED_LOCAL or domain in {
        "example.com", "example.org", "example.net", "example", "test"
    }:
        return False
    if domain.endswith((".test", ".example")) or domain.startswith("example."):
        return False
    if domain in {"sentry.io", "ingest.sentry.io"} or _TRACKING_LOCAL.fullmatch(local):
        return False
    if re.search(r"\.(?:png|jpe?g|gif|svg|webp|css|js)$", domain):
        return False
    return True


def classify_host(
    url: str,
    *,
    company_domain: str | None = None,
    host_kinds_path: Path = _DEFAULT_HOST_KINDS,
) -> HostKind:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in {"github.com", "www.github.com"} and len(
        [segment for segment in parts.path.split("/") if segment]
    ) == 1:
        return "github"
    if host in {"gitlab.com", "www.gitlab.com"} and len(
        [segment for segment in parts.path.split("/") if segment]
    ) == 1:
        return "gitlab"
    if host.startswith("lists."):
        return "mail_archive"
    if company_domain and registrable_domain(host) == registrable_domain(company_domain):
        return "company"
    try:
        data = tomllib.loads(host_kinds_path.read_text(encoding="utf-8"))
    except OSError:
        data = {}
    groups = data.get("host_kinds", {})
    for domain in groups.get("talk_domains", []):
        if host == domain or host.endswith("." + domain):
            return "talk"
    for domain in groups.get("personal_site_domains", []):
        if host == domain or host.endswith("." + domain):
            return "personal_site"
    if host.endswith(".github.io") or host.endswith(".gitlab.io"):
        return "personal_site"
    return "other"
