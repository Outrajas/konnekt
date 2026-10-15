from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEADFORGE_",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    input_dir: Path = Path("data/input")
    output_dir: Path = Path("data/output")
    cache_dir: Path = Path("data/cache")

    smtp_timeout: float = 10.0
    smtp_from_address: str = "verify@example.com"
    smtp_max_concurrent: int = 5

    request_delay_seconds: float = 1.0
    min_confidence_to_emit: float = 0.4


settings = Settings()

for _d in (settings.input_dir, settings.output_dir, settings.cache_dir):
    _d.mkdir(parents=True, exist_ok=True)
