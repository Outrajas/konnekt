import gzip
from pathlib import Path

import pytest

from leadforge.sources.search import parse_duckduckgo_results

FIXTURE = Path("tests/fixtures/search/ddg_stripe_engineering_blog.html.gz")


def test_real_snapshot_parses_search_results_without_fetching_them() -> None:
    markup = gzip.decompress(FIXTURE.read_bytes()).decode("utf-8")
    hits = parse_duckduckgo_results(markup)
    assert len(hits) >= 5
    assert hits[0].title == "Stripe | Dev Blog"
    assert hits[0].url == "https://stripe.dev/blog/topic/engineering"
    assert "Stripe" in hits[0].snippet
    assert all(hit.url.startswith(("http://", "https://")) for hit in hits)


def test_parser_tolerates_class_variants_and_deduplicates() -> None:
    markup = """
    <div data-testid="result"><h2><a data-testid="result-title-a"
      href="https://example.invalid/blog">Engineering blog</a></h2>
      <div data-testid="result-snippet">Company engineering posts</div></div>
    <div class="web-result"><h2><a class="result__a"
      href="https://example.invalid/blog">Duplicate</a></h2></div>
    """
    hits = parse_duckduckgo_results(markup)
    assert len(hits) == 1
    assert hits[0].url == "https://example.invalid/blog"
    assert hits[0].snippet == "Company engineering posts"


def test_parser_reports_unrecognised_markup() -> None:
    with pytest.raises(ValueError, match="unrecognised DuckDuckGo"):
        parse_duckduckgo_results("<html><body><p>not a result page</p></body></html>")
