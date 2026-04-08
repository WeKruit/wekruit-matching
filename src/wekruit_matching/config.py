"""Application configuration via pydantic-settings.

Reads from environment variables (set by ATM in production) or .env file locally.
Raises ValidationError at import time if required variables are missing.
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(...)
    anthropic_api_key: str = Field(..., repr=False)
    openai_api_key: str = Field(..., repr=False)
    github_token: str = Field(..., repr=False)
    log_level: str = Field("INFO")
    api_secret_key: str = Field(..., repr=False)

    # SiliconFlow (free-tier Qwen3-8B for enrichment classification)
    siliconflow_api_key: str = Field(..., repr=False)

    # Mailgun (optional — pipeline runs without it, just skips email)
    mailgun_api_key: str = Field("", repr=False)
    mailgun_domain: str = Field("wekruit.com")
    pipeline_notify_email: str = Field("admin1@wekruit.com")

    # Firecrawl (optional — daily pipeline skips this stage when unset)
    firecrawl_api_key: str = Field("", repr=False)
    firecrawl_base_url: str = Field("https://api.firecrawl.dev")

    # Serper.dev (optional — URL resolution fallback, 2500 free queries/month)
    serper_api_key: str = Field("", repr=False)

    # Firebase job sync (Phase 21)
    firebase_sync_url: str = Field("")
    firebase_sync_api_key: str = Field("", repr=False)
    firebase_sync_batch_size: int = Field(200)
    firebase_sync_timeout_seconds: float = Field(30.0)
    firebase_sync_collection: str = Field("matching-jobs")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance. Import and call this instead of Settings() directly."""
    return Settings()
