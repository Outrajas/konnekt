from types import SimpleNamespace

from typer.testing import CliRunner

from leadforge import cli


def test_fetch_prints_final_metadata_and_empty_text(monkeypatch):
    result = SimpleNamespace(
        final_url="https://fixture.example/final",
        status=200,
        tier=1,
        from_cache=True,
        html="",
    )
    monkeypatch.setattr(
        cli, "Fetcher", lambda _settings: SimpleNamespace(fetch=lambda *args, **kwargs: result)
    )
    monkeypatch.setattr(cli.asyncio, "run", lambda _awaitable: result)
    output = CliRunner().invoke(cli.app, ["fetch", "https://fixture.example/start", "--text"])
    assert output.exit_code == 0
    assert "final_url=https://fixture.example/final" in output.stdout
    assert "status=200" in output.stdout
    assert "tier=1" in output.stdout
    assert "blocked=False" in output.stdout
    assert "from_cache=True" in output.stdout
    assert "empty body" in output.stdout
