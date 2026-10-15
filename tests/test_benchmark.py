import asyncio

from leadforge.config import Settings
from scripts.benchmark_tiers import benchmark, read_domains


def test_read_domains_normalizes_urls_and_skips_comments(tmp_path) -> None:
    path = tmp_path / "domains.txt"
    path.write_text("# sample\nexample.org\nhttps://example.net/about\nexample.org\n")

    assert read_domains(path) == ["example.org", "example.net"]


def test_benchmark_dry_run_never_constructs_fetcher(tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "benchmark.db",
        domain_policy_path=tmp_path / "policy.toml",
    )
    settings.domain_policy_path.write_text(
        "[default]\nenabled=true\ndaily_cap=10\nmax_tier=1\nobey_robots=false\n"
    )

    async def run():
        return await benchmark(["example.org"], settings=settings, dry_run=True)

    [row] = asyncio.run(run())

    assert row.domain == "example.org"
    assert row.tier == "dry-run"
    assert row.classification == "not requested"
    assert row.latency_ms is None
