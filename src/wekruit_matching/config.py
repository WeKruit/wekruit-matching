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

    database_url: str = Field(
        ...,
        description="PostgreSQL connection string (psycopg3 format: postgresql+psycopg://...)",
    )
    anthropic_api_key: str = Field(..., description="Anthropic API key for LLM enrichment")
    openai_api_key: str = Field(..., description="OpenAI API key for embedding generation")
    github_token: str = Field(..., description="GitHub PAT for authenticated raw file fetches")
    log_level: str = Field("INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance. Import and call this instead of Settings() directly."""
    return Settings()
