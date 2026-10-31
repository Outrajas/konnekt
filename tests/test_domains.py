"""Synthetic (board-invented) domains must never reach enrichment."""

from __future__ import annotations

from leadforge.sources.domains import (
    is_synthetic_domain,
    load_domain_overrides,
    resolve_domain,
    sql_synthetic_filter,
)


def test_board_placeholders_are_synthetic() -> None:
    for domain in (
        "acme.wellfound",
        "ao-garcia-agency.remoteok",
        "acme.naukri",
        "acme.weworkremotely",
        "acme.test",
        "acme.example",
        "acme.invalid",
    ):
        assert is_synthetic_domain(domain), domain


def test_real_domains_are_not_synthetic() -> None:
    for domain in ("stripe.com", "acme.co.in", "acme.ai", "sub.example.org"):
        assert not is_synthetic_domain(domain), domain


def test_empty_domain_is_synthetic() -> None:
    assert is_synthetic_domain("")
    assert is_synthetic_domain(None)


def test_sql_filter_excludes_every_placeholder() -> None:
    sql = sql_synthetic_filter("c.domain")
    assert sql.startswith("AND c.domain IS NOT NULL AND c.domain != ''")
    assert "NOT LIKE '%.wellfound'" in sql
    assert "NOT LIKE '%.remoteok'" in sql
    assert "NOT LIKE '%.test'" in sql


def test_overrides_map_placeholder_to_real_domain(tmp_path) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(
        '{"acme.wellfound": "acme.ai", "_comment": "ignored"}', encoding="utf-8"
    )
    overrides = load_domain_overrides(path)
    assert overrides == {"acme.wellfound": "acme.ai"}
    assert resolve_domain("ACME.wellfound", overrides) == "acme.ai"
    assert resolve_domain("acme.remoteok", overrides) == ""
    assert resolve_domain("stripe.com", overrides) == "stripe.com"


def test_missing_override_file_is_not_fatal(tmp_path) -> None:
    assert load_domain_overrides(tmp_path / "absent.json") == {}
