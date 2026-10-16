"""Offline extraction from operator-saved browser pages."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect, init_schema
from leadforge.llm.schemas import PageExtraction, PersonMention
from leadforge.llm.validate import validate_page_extraction

SUPPORTED_SUFFIXES = {".html", ".mhtml", ".txt"}
PageKind = Literal["linkedin", "indeed_job", "glassdoor_job", "glassdoor_company", "company"]


class ImportedFields(BaseModel):
    """Strict LLM fallback schema: only approved professional fields are representable."""

    model_config = ConfigDict(extra="forbid")
    kind: PageKind
    name: str | None = None
    title: str | None = None
    company: str | None = None
    job_title: str | None = None
    location: str | None = None
    posting_url: str | None = None


@dataclass
class ImportedPage:
    path: Path
    kind: str
    status: str
    name: str | None = None
    title: str | None = None
    company: str | None = None
    job_title: str | None = None
    location: str | None = None
    posting_url: str | None = None
    canonical_url: str | None = None
    evidence: list[tuple[str, str]] = field(default_factory=list)


def saved_page_paths(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES),
        key=lambda p: str(p).casefold(),
    )


def parse_saved_page(path: Path) -> ImportedPage:
    """Parse only work-related values and report uncertain markup as unrecognised."""
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return ImportedPage(path, "unknown", "unrecognised page")
    source = _read_source(path)
    soup = BeautifulSoup(source, "lxml")
    text = " ".join(soup.stripped_strings)
    source_text = source if path.suffix.lower() == ".txt" else "\n".join(soup.stripped_strings)
    canonical = _canonical(soup)
    site = " ".join(
        [
            soup.title.get_text(" ", strip=True) if soup.title else "",
            _meta(soup).get("og:site_name", ""),
        ]
    ).casefold()
    hint = f"{site} {canonical or ''} {text[:5000]}".casefold()
    structured = _structured_data(soup)
    kind = _detect_kind(hint, structured)
    if kind == "unknown":
        return ImportedPage(path, "unknown", "unrecognised page", canonical_url=canonical)
    values = _extract(kind, soup, structured, canonical, source_text)
    if not any(values.values()):
        return ImportedPage(path, kind, "unrecognised page", canonical_url=canonical)
    page = ImportedPage(path, kind, "recognized", canonical_url=canonical, **values)
    page.evidence = [
        (key, value)
        for key in ("name", "title", "company", "job_title", "location", "posting_url")
        if (value := getattr(page, key))
    ]
    return page


async def parse_saved_page_with_llm(path: Path, llm_fn) -> ImportedPage:
    """Use an injected LLM only to fill deterministic misses, then cross-check every claim.

    The CLI intentionally does not supply a remote LLM because import-pages promises
    zero network calls. This adapter is for an explicitly supplied offline/local model.
    """
    page = parse_saved_page(path)
    if page.kind == "unknown":
        return page
    required = {
        "linkedin": ("name", "title"),
        "indeed_job": ("company", "job_title"),
        "glassdoor_job": ("company", "job_title"),
        "glassdoor_company": ("company",),
        "company": ("company",),
    }[page.kind]
    if page.status == "recognized" and all(getattr(page, key) for key in required):
        return page
    source = _read_source(path)
    soup = BeautifulSoup(source, "lxml")
    visible = " ".join(soup.stripped_strings).casefold()
    canonical = _canonical(soup)
    links = [url for node in soup.select("a[href]") if (url := _safe_url(str(node.get("href"))))]
    proposed = llm_fn(
        page_kind=page.kind,
        text=" ".join(soup.stripped_strings),
        canonical_url=canonical,
        links=links,
    )
    if inspect.isawaitable(proposed):
        proposed = await proposed
    if isinstance(proposed, BaseModel):
        proposed = proposed.model_dump()
    try:
        fields = ImportedFields.model_validate(proposed)
    except Exception:
        return page
    if fields.kind != page.kind:
        return page
    values = fields.model_dump(exclude={"kind"})
    if fields.name:
        checked = validate_page_extraction(
            PageExtraction(
                page_kind="other",
                people=[PersonMention(name=fields.name, title=fields.title, professional=True)],
            ),
            cleaned_text=visible,
            extracted_emails=[],
            candidate_links=links,
            page_host=urlsplit(canonical or "").hostname or "",
        )
        if not checked.people:
            return page
    for key, value in values.items():
        if value is None:
            continue
        if key == "posting_url":
            if value not in links and value != canonical:
                return page
        elif value.casefold() not in visible:
            return page
    for key, value in values.items():
        if getattr(page, key) is None:
            setattr(page, key, value)
    page.status = "recognized" if any(getattr(page, key) for key in values) else page.status
    page.evidence = [
        (key, value)
        for key in ("name", "title", "company", "job_title", "location", "posting_url")
        if (value := getattr(page, key))
    ]
    return page


def persist_import(page: ImportedPage, settings: Settings) -> None:
    if page.status != "recognized":
        return
    now = clock.utcnow_iso()
    domain = _domain_for(page)
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        company_id = _upsert_company(conn, page.company, domain, now)
        person_id = None
        if page.name:
            name_parts = page.name.split()
            first = name_parts[0]
            last = " ".join(name_parts[1:])
            existing = conn.execute(
                "SELECT id FROM person WHERE company_id IS ? AND first_name=? AND "
                "last_name=? LIMIT 1",
                (company_id, first, last),
            ).fetchone()
            if existing:
                person_id = int(existing["id"])
                conn.execute(
                    "UPDATE person SET title=COALESCE(?,title),updated_at=? WHERE id=?",
                    (page.title, now, person_id),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO person(company_id,first_name,last_name,title,status,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (company_id, first, last, page.title, "imported", now, now),
                )
                person_id = cursor.lastrowid
        if page.job_title:
            conn.execute(
                "INSERT INTO job(company_id,title,url,location,source,first_seen,last_seen) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,"
                "location=excluded.location,last_seen=excluded.last_seen",
                (company_id, page.job_title, page.posting_url, page.location, page.kind, now, now),
            )
        for key, value in page.evidence:
            conn.execute(
                "INSERT INTO evidence(person_id,company_id,kind,value,professional,created_at) "
                "VALUES(?,?,?,?,1,?)",
                (person_id, company_id, f"manual_{key}", value, now),
            )
        conn.commit()
    finally:
        conn.close()


def _upsert_company(conn, name: str | None, domain: str | None, now: str) -> int | None:
    if not name and not domain:
        return None
    if domain:
        existing = conn.execute("SELECT id FROM company WHERE domain=?", (domain,)).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM company WHERE name=? COLLATE NOCASE AND domain IS NULL LIMIT 1",
            (name,),
        ).fetchone()
    if existing:
        company_id = int(existing["id"])
        if name:
            conn.execute("UPDATE company SET name=? WHERE id=?", (name, company_id))
        return company_id
    cursor = conn.execute(
        "INSERT INTO company(name,domain,source,created_at) VALUES(?,?,?,?)",
        (name or domain or "Saved page", domain, "manual-page", now),
    )
    return cursor.lastrowid


def _domain_for(page: ImportedPage) -> str | None:
    host = urlsplit(page.canonical_url or "").hostname
    if host and not any(part in host for part in ("linkedin.", "indeed.", "glassdoor.")):
        return host.lower()
    return None


def _detect_kind(hint: str, structured: list[dict[str, Any]]) -> str:
    types = " ".join(str(item.get("@type", "")) for item in structured).casefold()
    if "linkedin" in hint:
        return "linkedin"
    if "indeed" in hint:
        return "indeed_job"
    if "glassdoor" in hint:
        return "glassdoor_job" if ("jobposting" in types or "job" in hint) else "glassdoor_company"
    if "organization" in types:
        return "company"
    return "unknown"


def _extract(
    kind: str,
    soup,
    structured: list[dict[str, Any]],
    canonical: str | None,
    source_text: str,
):
    data = next(
        (item for item in structured if "jobposting" in str(item.get("@type", "")).casefold()),
        {},
    )
    if kind == "linkedin":
        card = soup.select_one(".entity-result__item, [data-view-name*='search-entity-result']")
        root = card or soup
        labeled = _labeled_fields(source_text)
        return {
            "name": _text(root, "h1, .entity-result__title-text, [data-anonymize='person-name']")
            or labeled.get("name"),
            "title": _text(
                root,
                ".text-body-medium, .entity-result__primary-subtitle, [data-anonymize='headline']",
            )
            or labeled.get("title"),
            "company": _text(
                root, ".entity-result__secondary-subtitle, [data-anonymize='company-name']"
            )
            or labeled.get("company"),
            "job_title": None,
            "location": None,
            "posting_url": None,
        }
    if kind in {"indeed_job", "glassdoor_job"}:
        labeled = _labeled_fields(source_text)
        return {
            "name": None,
            "title": None,
            "company": _name(data.get("hiringOrganization"))
            or _text(soup, "[data-company-name], .companyName, [class*='employer']")
            or labeled.get("company"),
            "job_title": _string(data.get("title"))
            or _text(soup, "h1, [data-testid*='jobTitle'], [class*='JobTitle']")
            or labeled.get("job title"),
            "location": _location(data.get("jobLocation"))
            or _text(soup, "[data-testid*='location'], [class*='location']")
            or labeled.get("location"),
            "posting_url": _safe_url(
                _string(data.get("url")) or canonical or labeled.get("posting url")
            ),
        }
    company = (
        _name(data.get("name")) or _text(soup, "h1") or _labeled_fields(source_text).get("company")
    )
    return {
        "name": None,
        "title": None,
        "company": company,
        "job_title": None,
        "location": None,
        "posting_url": None,
    }


def _structured_data(soup) -> list[dict[str, Any]]:
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        found.extend(_flatten(payload))
    return found


def _read_source(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix.lower() == ".mhtml":
        message = BytesParser(policy=policy.default).parsebytes(raw)
        return "\n".join(
            str(part.get_content())
            for part in message.walk()
            if part.get_content_type() == "text/html"
        )
    return raw.decode("utf-8", errors="replace")


def _flatten(value):
    if isinstance(value, list):
        return [item for child in value for item in _flatten(child)]
    if isinstance(value, dict):
        result = [value]
        if value.get("@graph"):
            result.extend(_flatten(value["@graph"]))
        return result
    return []


def _text(root, selector):
    node = root.select_one(selector)
    return " ".join(node.stripped_strings).strip() if node else None


def _meta(soup):
    return {
        str(node.get("property") or node.get("name")).casefold(): str(node.get("content"))
        for node in soup.select("meta[property], meta[name]")
        if node.get("content")
    }


def _canonical(soup):
    node = soup.select_one('link[rel~="canonical"]')
    return _safe_url(str(node.get("href"))) if node and node.get("href") else None


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    return value.strip() if parts.scheme in {"http", "https"} and parts.hostname else None


def _name(value):
    if isinstance(value, dict):
        return _string(value.get("name"))
    return _string(value)


def _string(value):
    return str(value).strip() or None if value is not None else None


def _location(value):
    if isinstance(value, list):
        return ", ".join(filter(None, (_location(item) for item in value))) or None
    if isinstance(value, dict):
        address = value.get("address", value)
        if isinstance(address, dict):
            fields = ("addressLocality", "addressRegion", "addressCountry")
            return (
                ", ".join(str(address[key]).strip() for key in fields if address.get(key)) or None
            )
    return None


def _labeled_fields(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        normalized = " ".join(key.casefold().split())
        value = " ".join(value.split())
        if (
            separator
            and normalized in {"name", "title", "company", "job title", "location", "posting url"}
            and value
        ):
            fields[normalized] = value
    return fields
