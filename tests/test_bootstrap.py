"""Bootstrap and domain-resolution logic that does not need the network."""

from __future__ import annotations

import csv

from leadforge.bootstrap import (
    STARTER_COMPANIES,
    ensure_env_file,
    missing_inputs,
    write_starter_companies,
)
from leadforge.config import Settings
from leadforge.stages.domains_resolve import page_mentions, slug_candidates


def test_slug_candidates_prefer_compact_then_trimmed() -> None:
    candidates = slug_candidates("Sarvam AI")
    assert candidates[0] == "sarvamai.com"
    assert "sarvam.com" in candidates
    assert all(" " not in candidate for candidate in candidates)


def test_slug_candidates_drop_legal_suffixes() -> None:
    candidates = slug_candidates("Acme Technologies Private Limited")
    assert "acme.com" in candidates


def test_slug_candidates_empty_for_symbols_only() -> None:
    assert slug_candidates("---") == []


def test_page_mentions_accepts_a_naming_homepage() -> None:
    html = "<title>Acme Robotics</title><h1>Acme Robotics builds arms</h1>"
    assert page_mentions(html, "Acme Robotics")


def test_page_mentions_rejects_an_unrelated_homepage() -> None:
    assert not page_mentions("<html>Domain parked</html>", "Acme Robotics")


def test_write_starter_companies_creates_file(tmp_path) -> None:
    path = tmp_path / "companies.csv"
    added = write_starter_companies(path)
    assert added == len(STARTER_COMPANIES)
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {"name", "domain"} <= set(rows[0])
    assert len(rows) == len(STARTER_COMPANIES)


def test_write_starter_companies_preserves_operator_rows(tmp_path) -> None:
    path = tmp_path / "companies.csv"
    path.write_text(
        "name,domain\nMy Own Startup,mystartup.example.org\nSarvam AI,override.ai\n",
        encoding="utf-8",
    )
    write_starter_companies(path)
    with path.open(encoding="utf-8") as handle:
        rows = {r["name"]: r["domain"] for r in csv.DictReader(handle)}
    assert rows["My Own Startup"] == "mystartup.example.org"
    assert rows["Sarvam AI"] == "override.ai"  # operator edit wins


def test_ensure_env_file_copies_template_once(tmp_path) -> None:
    (tmp_path / ".env.free.example").write_text("LEADFORGE_PROXY_URL=\n", encoding="utf-8")
    assert ensure_env_file(tmp_path) is True
    assert (tmp_path / ".env").exists()
    assert ensure_env_file(tmp_path) is False  # second run is a no-op


def test_ensure_env_file_does_not_clobber_existing(tmp_path) -> None:
    (tmp_path / ".env.free.example").write_text("X=1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=keep\n", encoding="utf-8")
    assert ensure_env_file(tmp_path) is False
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "SECRET=keep\n"


def test_missing_inputs_flags_llm_when_unset(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    missing = missing_inputs(settings)
    assert any("LLM" in item for item in missing)
    assert any("companies" in item for item in missing)
