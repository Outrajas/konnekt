from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEADFORGE_",
        env_ignore_empty=True,
        extra="ignore",
    )

    data_dir: Path = Path("data")
    input_dir: Path = Path("data/input")
    output_dir: Path = Path("data/output")
    cache_dir: Path = Path("data/cache")
    db_path: Path = Path("data/cache/leadforge.db")
    domain_policy_path: Path = Path("data/domain_policy.toml")
    denylist_hosts_path: Path = Path("data/denylist_hosts.txt")

    smtp_mail_from: str | None = None
    smtp_timeout: float = 10.0
    smtp_timeout_seconds: float = 10.0
    smtp_from_address: str = "verify@example.com"
    smtp_max_concurrent: int = 5

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

    obscura_path: Path | None = None
    obscura_stealth: bool = True
    obscura_timeout_seconds: int = 30

    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_max_input_chars: int = 12_000
    llm_timeout_seconds: float = 60.0
    llm_requests_per_minute: int = 10
    llm_daily_cap: int = 100

    retention_days: int = 30


settings = Settings()

for _d in (settings.input_dir, settings.output_dir, settings.cache_dir):
    _d.mkdir(parents=True, exist_ok=True)
