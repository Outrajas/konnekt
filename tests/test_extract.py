from pathlib import Path

import pytest

from leadforge.web.extract import (
    classify_host,
    clean_text,
    extract_emails,
    extract_links,
    registrable_domain,
)

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Contact: jane [at] acme [dot] com", "jane@acme.com"),
        ("Email Jane (at) acme.com", "jane@acme.com"),
        ("Reach Jane at acme dot com", "jane@acme.com"),
        ("Contact: jane(at)acme.com", "jane@acme.com"),
    ],
)
def test_obfuscation_variants_need_contact_context(source, expected) -> None:
    found = extract_emails(source)
    assert [email.email for email in found] == [expected]
    assert found[0].raw_match


@pytest.mark.parametrize(
    "address",
    [
        "jane@example.com",
        "jane@example.org",
        "jane@example.net",
        "jane@sample.test",
        "noreply@acme.com",
        "no-reply@acme.com",
        "photo@2x.png",
        "deadbeef42@acme.com",
        "abc123@sentry.io",
    ],
)
def test_email_false_positives_are_filtered(address) -> None:
    assert extract_emails(address) == []


def test_obfuscated_address_without_local_contact_context_is_ignored() -> None:
    assert extract_emails("jane [at] acme [dot] com") == []


def test_plain_address_and_mailto_query_are_extracted_once() -> None:
    result = extract_emails(
        '<a href="mailto:jane%40acme.com?subject=Hello&body=Hi">jane@acme.com</a>'
    )
    assert len(result) == 1
    assert result[0].email == "jane@acme.com"
    assert result[0].raw_match


def test_unicode_names_survive_clean_text() -> None:
    text = clean_text("<html><body><p>Zoë García — Staff Engineer</p></body></html>")
    assert "Zoë García" in text
    assert "Staff Engineer" in text


def test_clean_text_fallback_and_paragraph_boundary_truncation() -> None:
    html = "<html><body><p>Alpha</p><p>Bravo</p><p>Charlie</p></body></html>"
    assert clean_text(html, max_chars=8) == "Alpha"
    assert clean_text(html, max_chars=12) == "Alpha\n\nBravo"


def test_extract_links_resolves_relative_urls_and_navigation_metadata() -> None:
    links = extract_links(
        '<nav><a href="/team" rel="nofollow">Team</a></nav>'
        '<a href="../contact#email">Contact</a>',
        "https://jobs.acme.com/company/about",
        denylist=set(),
    )
    assert links[0].url == "https://jobs.acme.com/team"
    assert links[0].rel == ("nofollow",)
    assert links[0].same_domain is True
    assert links[0].in_navigation is True
    assert links[1].url == "https://jobs.acme.com/contact"
    assert links[1].in_navigation is False


def test_extract_links_deduplicates_and_drops_non_http_fragment_and_denied_hosts() -> None:
    html = (
        '<a href="#section">jump</a><a href="javascript:alert(1)">js</a>'
        '<a href="mailto:a@acme.com">mail</a>'
        '<a href="https://facebook.com/jane">social</a>'
        '<a href="/same">one</a><a href="/same#fragment">duplicate</a>'
    )
    links = extract_links(html, "https://acme.com/", denylist={"facebook.com"})
    assert [link.url for link in links] == ["https://acme.com/same"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.acme.co.uk", "acme.co.uk"),
        ("jobs.linkedin.com", "linkedin.com"),
        ("localhost", "localhost"),
    ],
)
def test_registrable_domain(host, expected) -> None:
    assert registrable_domain(host) == expected


@pytest.mark.parametrize(
    ("url", "company_domain", "expected"),
    [
        ("https://github.com/janedoe", None, "github"),
        ("https://github.com/janedoe/project", None, "other"),
        ("https://gitlab.com/janedoe", None, "gitlab"),
        ("https://lists.acme.com/archive", None, "mail_archive"),
        ("https://www.usenix.org/conference", None, "talk"),
        ("https://team.acme.com/about", "acme.com", "company"),
        ("https://jane.example-site.com", None, "other"),
        ("https://jane.github.io/about", None, "personal_site"),
    ],
)
def test_classify_host(url, company_domain, expected) -> None:
    assert classify_host(url, company_domain=company_domain) == expected


def test_sample_html_fixture_extracts_visible_content_and_addresses() -> None:
    html = (ROOT / "tests" / "fixtures" / "html" / "extract_sample.html").read_text(
        encoding="utf-8"
    )
    assert "Jane Doe" in clean_text(html, "https://acme.example/")
    assert extract_emails(html) == []
