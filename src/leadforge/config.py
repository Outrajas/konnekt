import os
from pathlib import Path
from types import MethodType
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_SETTING_ENV_NAMES: dict[str, str] = {}
_SETTING_ENV_NAMES_PRESENT: dict[str, set[str]] = {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEADFORGE_",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Path("data")
    input_dir: Path = Path("data/input")
    output_dir: Path = Path("data/output")
    cache_dir: Path = Path("data/cache")
    db_path: Path = Path("data/cache/leadforge.db")
    domain_policy_path: Path = Path("data/domain_policy.toml")
    denylist_hosts_path: Path = Path("data/denylist_hosts.txt")

    smtp_mail_from: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SMTP_MAIL_FROM", "LEADFORGE_SMTP_MAIL_FROM"),
    )
    smtp_verify_mode: Literal["off", "null_sender", "domain"] = "off"
    smtp_timeout: float = 10.0
    smtp_timeout_seconds: float = 10.0
    smtp_max_concurrent: int = 5
    dns_nameservers: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("DNS_NAMESERVERS", "LEADFORGE_DNS_NAMESERVERS"),
    )

    request_delay_seconds: float = 1.0
    min_confidence_to_emit: float = 0.4

    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    http_timeout_seconds: float = 20.0
    cache_ttl_days: int = 14
    default_domain_min_delay_s: float = 3.0
    default_domain_max_delay_s: float = 8.0
    default_domain_daily_cap: int = 30
    pause_hours_on_block: int = 48

    walk_max_fetches: int = 8
    walk_max_depth: int = 3
    walk_stale_hops: int = 2
    walk_wall_clock_seconds: int = 600

    obscura_path: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("OBSCURA_PATH", "LEADFORGE_OBSCURA_PATH"),
    )
    obscura_stealth: bool = Field(
        default=True,
        validation_alias=AliasChoices("OBSCURA_STEALTH", "LEADFORGE_OBSCURA_STEALTH"),
    )
    obscura_timeout_seconds: int = Field(
        default=30,
        validation_alias=AliasChoices(
            "OBSCURA_TIMEOUT_SECONDS", "LEADFORGE_OBSCURA_TIMEOUT_SECONDS"
        ),
    )

    llm_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("LLM_BASE_URL", "LEADFORGE_LLM_BASE_URL")
    )
    llm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("LLM_API_KEY", "LEADFORGE_LLM_API_KEY")
    )
    llm_model: str | None = Field(
        default=None, validation_alias=AliasChoices("LLM_MODEL", "LEADFORGE_LLM_MODEL")
    )
    llm_max_input_chars: int = Field(
        default=12_000,
        validation_alias=AliasChoices("LLM_MAX_INPUT_CHARS", "LEADFORGE_LLM_MAX_INPUT_CHARS"),
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS", "LEADFORGE_LLM_TIMEOUT_SECONDS"),
    )
    llm_requests_per_minute: int = Field(
        default=10,
        validation_alias=AliasChoices(
            "LLM_REQUESTS_PER_MINUTE", "LEADFORGE_LLM_REQUESTS_PER_MINUTE"
        ),
    )
    llm_daily_cap: int = Field(
        default=100,
        validation_alias=AliasChoices("LLM_DAILY_CAP", "LEADFORGE_LLM_DAILY_CAP"),
    )

    searxng_base_url: str | None = None

    retention_days: int = 30

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        """Track the selected environment variable name without retaining or displaying values."""
        for source in (env_settings, dotenv_settings):
            original = source.get_field_value

            def tracked(self, field, field_name, _original=original):
                value, key, complex_value = _original(field, field_name)
                if field_name in {
                    "smtp_mail_from",
                    "dns_nameservers",
                    "obscura_path",
                    "obscura_stealth",
                    "obscura_timeout_seconds",
                    "llm_base_url",
                    "llm_api_key",
                    "llm_model",
                    "llm_max_input_chars",
                    "llm_timeout_seconds",
                    "llm_requests_per_minute",
                    "llm_daily_cap",
                }:
                    aliases = []
                    validation_alias = getattr(field, "validation_alias", None)
                    if isinstance(validation_alias, AliasChoices):
                        aliases.extend(str(alias) for alias in validation_alias.choices)
                    for _field_key, env_name, _is_complex in self._extract_field_info(
                        field, field_name
                    ):
                        prefix = str(self.config.get("env_prefix") or "")
                        candidates = [env_name, *aliases]
                        if prefix and not env_name.upper().startswith(prefix.upper()):
                            candidates.append(prefix + env_name)
                        available = {name.upper() for name in self.env_vars}
                        available.update(name.upper() for name in os.environ)
                        present = _SETTING_ENV_NAMES_PRESENT.setdefault(field_name, set())
                        present.update(
                            name.upper() for name in candidates if name.upper() in available
                        )
                        found_name = next(
                            (name.upper() for name in candidates if name.upper() in available),
                            None,
                        )
                        if found_name:
                            _SETTING_ENV_NAMES.setdefault(field_name, found_name)
                            break
                    if value is not None:
                        _SETTING_ENV_NAMES.setdefault(field_name, str(key).upper())
                return value, key, complex_value

            source.get_field_value = MethodType(tracked, source)
        return init_settings, env_settings, dotenv_settings, file_secret_settings


def setting_env_name(field_name: str) -> str | None:
    """Return only the selected setting variable's name, never its value."""
    return _SETTING_ENV_NAMES.get(field_name)


def setting_env_names(field_name: str) -> tuple[str, ...]:
    """Return only the names of supplied aliases for a setting."""
    return tuple(sorted(_SETTING_ENV_NAMES_PRESENT.get(field_name, set())))


settings = Settings()

for _d in (settings.input_dir, settings.output_dir, settings.cache_dir):
    _d.mkdir(parents=True, exist_ok=True)
