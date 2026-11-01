"""ProxyPool: round-robin selection, failure parking, file loading."""

from __future__ import annotations

from leadforge.web.proxy import ProxyPool, parse_proxy


def _pool(*entries: str, cooldown: int = 2) -> ProxyPool:
    proxies = [parse_proxy(entry) for entry in entries]
    return ProxyPool([p for p in proxies if p is not None], cooldown=cooldown)


def test_round_robin_cycles_in_order() -> None:
    pool = _pool("http://a.test:1", "http://b.test:2")
    seen = [pool.next().server for _ in range(4)]
    assert seen == ["http://a.test:1", "http://b.test:2", "http://a.test:1", "http://b.test:2"]


def test_empty_pool_returns_none() -> None:
    assert ProxyPool([]).next() is None


def test_mark_bad_parks_a_proxy_for_the_cooldown() -> None:
    pool = _pool("http://a.test:1", "http://b.test:2", cooldown=1)
    first = pool.next()
    assert first.server == "http://a.test:1"
    pool.mark_bad(first)
    # a.test is parked for one selection: the next pick must skip it
    assert pool.next().server == "http://b.test:2"
    # after the cooldown tick it is eligible again
    seen = {pool.next().server for _ in range(4)}
    assert "http://a.test:1" in seen


def test_failure_counter_is_exposed_for_health_output() -> None:
    pool = _pool("http://a.test:1")
    pool.mark_bad(pool.next())
    assert pool.health() == [("http://a.test:1", 1)]


def test_load_reads_file_ignoring_comments_and_bad_lines(tmp_path) -> None:
    path = tmp_path / "proxies.txt"
    path.write_text(
        "# gate node\nhttp://user:pass@a.test:8080\n\nnot a proxy\nsocks5://b.test:1080\n",
        encoding="utf-8",
    )
    pool = ProxyPool.load(path, None)
    assert pool is not None
    assert len(pool) == 2
    assert {p.server for p in pool.proxies} == {
        "http://a.test:8080",
        "socks5://b.test:1080",
    }


def test_load_falls_back_to_inline_when_file_missing(tmp_path) -> None:
    pool = ProxyPool.load(tmp_path / "absent.txt", "http://c.test:3128")
    assert pool is not None and len(pool) == 1
    assert pool.next().server == "http://c.test:3128"


def test_load_returns_none_when_nothing_configured(tmp_path) -> None:
    assert ProxyPool.load(tmp_path / "absent.txt", None) is None


def test_load_ignores_empty_file_and_uses_inline(tmp_path) -> None:
    path = tmp_path / "proxies.txt"
    path.write_text("# nothing usable\n", encoding="utf-8")
    pool = ProxyPool.load(path, "socks5h://127.0.0.1:9050")
    assert pool is not None
    assert pool.next().server == "socks5h://127.0.0.1:9050"
