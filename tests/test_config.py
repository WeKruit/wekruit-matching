"""Tests for pydantic-settings config layer (FOUND-08)."""
import pytest
from pydantic import ValidationError


def test_settings_loads_from_env(monkeypatch, tmp_path):
    """Settings() reads DATABASE_URL and other required vars from env."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-test")
    # Isolate from the ambient environment: CI (ci.yml) and many shells export
    # LOG_LEVEL, which Settings would read over the default. This test asserts the
    # DEFAULT, so it must control the var rather than assume it is unset.
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    # Import inside test to avoid module-level .env read polluting other tests
    import importlib
    import wekruit_matching.config as cfg_module
    importlib.reload(cfg_module)
    from wekruit_matching.config import Settings

    s = Settings(_env_file=None)  # disable .env file read — use monkeypatched env vars only
    assert s.database_url == "postgresql+psycopg://user:pass@localhost:5432/test"
    assert s.anthropic_api_key == "sk-ant-test"
    assert s.log_level == "INFO"  # default (LOG_LEVEL not set in monkeypatch)
    assert s.matching_use_derived_experience is False


def test_settings_reads_derived_experience_feature_flag(monkeypatch):
    """MATCHING_USE_DERIVED_EXPERIENCE is opt-in and parsed as a bool."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-test")
    monkeypatch.setenv("MATCHING_USE_DERIVED_EXPERIENCE", "true")

    import importlib
    import wekruit_matching.config as cfg_module
    importlib.reload(cfg_module)
    from wekruit_matching.config import Settings

    s = Settings(_env_file=None)
    assert s.matching_use_derived_experience is True


def test_settings_raises_on_missing_database_url(monkeypatch):
    """Settings() raises ValidationError when DATABASE_URL is absent."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-test")

    import importlib
    import wekruit_matching.config as cfg_module
    importlib.reload(cfg_module)
    from wekruit_matching.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # disable .env file read to test env-only path
    assert "database_url" in str(exc_info.value).lower()


def test_settings_raises_on_missing_anthropic_key(monkeypatch):
    """Settings() raises ValidationError when ANTHROPIC_API_KEY is absent."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-test")

    import importlib
    import wekruit_matching.config as cfg_module
    importlib.reload(cfg_module)
    from wekruit_matching.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert "anthropic_api_key" in str(exc_info.value).lower()
