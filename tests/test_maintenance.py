import os
import time

from typer.testing import CliRunner

from leadforge import cli
from leadforge.config import Settings


def test_purge_older_than_removes_cached_page_files(tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "data" / "cache",
        db_path=tmp_path / "data" / "cache" / "leadforge.db",
    )
    page = settings.cache_dir / "pages" / "fictional-page.html"
    page.parent.mkdir(parents=True)
    page.write_text("fictional page body", encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(page, (old, old))
    monkeypatch.setattr(cli, "settings", settings)

    result = CliRunner().invoke(cli.app, ["purge", "--older-than", "30"])

    assert result.exit_code == 0, result.output
    assert not page.exists()
    assert "cache_files: 1" in result.output
