from scripts.check_tracked_files import tracked_file_violations


def test_forbidden_paths_are_reported(tmp_path) -> None:
    violations = tracked_file_violations(
        tmp_path,
        [
            ".env",
            "data/input/companies.csv",
            "data/profile.json",
            "bin/tool.exe",
            "run.db",
            ".venv/Lib/site-packages/example.py",
            ".venv-agent/Lib/site-packages/example.py",
        ],
    )
    assert len(violations) == 7


def test_gitkeep_is_allowed_but_api_key_pattern_is_rejected(tmp_path) -> None:
    (tmp_path / "data/input").mkdir(parents=True)
    (tmp_path / "data/input/.gitkeep").write_text("", encoding="utf-8")
    fake_key = "abcdefghijklmnopqrstuvwxyz" + "123456"
    (tmp_path / "settings.py").write_text(f'API_KEY = "{fake_key}"\n', encoding="utf-8")
    violations = tracked_file_violations(tmp_path, ["data/input/.gitkeep", "settings.py"])
    assert violations == ["settings.py: possible API key"]
