"""Role-title extraction from public company careers pages."""

from __future__ import annotations

from bs4 import BeautifulSoup

from leadforge.models import Job


def parse_careers_page(html: str, company: str, domain: str, url: str) -> list[Job]:
    soup = BeautifulSoup(html, "lxml")
    shell = not soup.get_text(" ", strip=True) and bool(soup.find("script"))
    if shell:
        return []
    seen: set[str] = set()
    jobs: list[Job] = []
    for heading in soup.find_all(["h1", "h2", "h3"]):
        title = " ".join(heading.get_text(" ", strip=True).split())
        if len(title) < 4 or title.lower() in seen:
            continue
        seen.add(title.lower())
        anchor = heading.find_parent("a") or heading.find_next("a", href=True)
        job_url = str(anchor.get("href")) if anchor else url
        if anchor and job_url.startswith("/"):
            from urllib.parse import urljoin

            job_url = urljoin(url, job_url)
        jobs.append(
            Job(
                company=company,
                domain=domain,
                title=title,
                url=job_url,
                source="careers-page",
            )
        )
    return jobs


def is_js_shell(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    return len(text) < 80 and bool(soup.find("script"))
