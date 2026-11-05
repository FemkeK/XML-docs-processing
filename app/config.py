"""Application configuration, read from environment variables (or a `.env` file)."""

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """All settings. Each field is read from the environment variable with the same name
    in upper case, for example `database_url` from DATABASE_URL."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Defaults to a local SQLite file so the app and tests run without Docker.
    # docker-compose overrides this with a PostgreSQL URL.
    database_url: str = "sqlite:///./documents.db"

    # Comma-separated list of accepted API keys (API_KEYS="key1,key2"). Empty means every
    # request is rejected, so a misconfigured deployment fails closed instead of open.
    # NoDecode: without it, pydantic-settings expects a list as JSON ('["key1"]').
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Upper bound for an uploaded document; protects memory and the XML parser.
    max_document_bytes: int = 1_000_000

    # Worker tuning.
    # How long an idle worker waits before checking the queue again.
    worker_poll_interval_seconds: float = 1.0
    # How long a claimed document belongs to one worker. After that, another worker
    # may take it over (the first one is assumed to have crashed).
    worker_lease_seconds: int = 60
    # Total number of attempts, including the first, before a document becomes FAILED.
    worker_max_attempts: int = 5
    # Wait before retry n: base * 2^(n-1) seconds, capped at the maximum.
    worker_backoff_base_seconds: float = 2.0
    worker_backoff_max_seconds: float = 60.0
    # Demo only: probability that processing raises a transient error, to show retries.
    simulated_failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    # The worker touches this file on every loop iteration. A healthcheck can then tell
    # from the file's age whether the loop is still running. Empty disables it.
    worker_heartbeat_file: str = ""

    log_level: str = "INFO"

    @field_validator("api_keys", mode="before")
    @classmethod
    def split_comma_separated(cls, value: str | list[str]) -> list[str]:
        """Turn "key1, key2" into ["key1", "key2"], skipping empty entries."""
        if isinstance(value, str):
            return [key.strip() for key in value.split(",") if key.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """The settings, read from the environment once and then reused."""
    return Settings()
