from pathlib import Path

from leadforge.config import (
    _SETTING_ENV_NAMES,
    _SETTING_ENV_NAMES_PRESENT,
    Settings,
    setting_env_name,
    setting_env_names,
)


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


def test_obscura_path_uses_operator_named_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv("OBSCURA_PATH", "bin/obscura/obscura.exe")
    settings = Settings(_env_file=None)
    assert settings.obscura_path == Path("bin/obscura/obscura.exe")


def test_unprefixed_and_prefixed_aliases_are_accepted_without_printing_values(monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    _SETTING_ENV_NAMES.pop("llm_model", None)
    _SETTING_ENV_NAMES_PRESENT.pop("llm_api_key", None)
    monkeypatch.setenv("LLM_API_KEY", "do-not-print-this")
    monkeypatch.setenv("LEADFORGE_LLM_API_KEY", "also-do-not-print-this")
    monkeypatch.setenv("LEADFORGE_LLM_MODEL", "test-model")
    monkeypatch.setenv("SMTP_MAIL_FROM", "sender@company.invalid")
    monkeypatch.setenv("DNS_NAMESERVERS", '["192.0.2.1"]')
    settings = Settings(_env_file=None)
    assert settings.llm_api_key is not None
    assert settings.llm_model == "test-model"
    assert settings.smtp_mail_from == "sender@company.invalid"
    assert settings.dns_nameservers == ["192.0.2.1"]
    assert setting_env_name("llm_api_key") == "LLM_API_KEY"
    assert setting_env_name("smtp_mail_from") == "SMTP_MAIL_FROM"
    assert setting_env_name("dns_nameservers") == "DNS_NAMESERVERS"
    assert setting_env_name("llm_model") == "LEADFORGE_LLM_MODEL"
    assert set(setting_env_names("llm_api_key")) == {"LLM_API_KEY", "LEADFORGE_LLM_API_KEY"}
    assert "do-not-print-this" not in repr(settings)


def test_empty_environment_alias_name_is_reported_without_its_value(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_MAIL_FROM", "")
    _SETTING_ENV_NAMES.pop("smtp_mail_from", None)
    settings = Settings(_env_file=None)
    assert settings.smtp_mail_from is None
    assert setting_env_name("smtp_mail_from") == "SMTP_MAIL_FROM"
