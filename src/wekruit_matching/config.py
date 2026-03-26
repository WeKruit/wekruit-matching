"""Application configuration via pydantic-settings.

Reads from .env file at project root. Raises ValidationError at import time
if required variables are missing — fail fast, not mid-execution.
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance. Import and call this instead of Settings() directly."""
    return Settings()
