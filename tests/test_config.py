from leadforge.config import Settings


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.walk_max_fetches == 8
    assert settings.walk_max_depth == 3
    assert settings.walk_stale_hops == 2
    assert settings.walk_wall_clock_seconds == 600
    assert settings.db_path.as_posix() == "data/cache/leadforge.db"
    assert settings.domain_policy_path.as_posix() == "data/domain_policy.toml"
    assert settings.llm_api_key is None


def test_secret_is_redacted_from_settings_repr() -> None:
    secret = "test-only-never-print-this-key"
    settings = Settings(_env_file=None, llm_api_key=secret)
    assert secret not in repr(settings)
