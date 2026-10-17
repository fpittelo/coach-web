"""Tests for coach_web.config."""

import pytest
from pydantic import SecretStr

from coach_web.config import Settings, get_settings


class TestSettings:
    """Settings loading and caching behaviour."""

    def test_default_values(self) -> None:
        """Default values are provided when env vars are absent."""
        get_settings.cache_clear()
        settings = get_settings()

        assert settings.COACH_MCP_URL == "http://coach-mcp:8000/sse"
        assert settings.GITHUB_MCP_URL == "http://github-mcp:8001/"
        assert settings.GITHUB_TOKEN == ""
        assert settings.GITHUB_REPO == "fpittelo/coach"
        assert settings.CACHE_TTL_SECONDS == 60
        assert settings.LOG_LEVEL == "INFO"
        assert settings.APP_HOST == "0.0.0.0"  # noqa: S104
        assert settings.APP_PORT == 8000
        assert settings.CORS_ORIGINS == ["http://localhost:8000"]

    def test_service_name_is_constant(self) -> None:
        """The service name identifies coach-web in the health payload."""
        get_settings.cache_clear()

        assert get_settings().service_name == "coach-web"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables override defaults."""
        monkeypatch.setenv("COACH_MCP_URL", "http://override.local/mcp")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_override")
        monkeypatch.setenv("GITHUB_REPO", "other/repo")
        monkeypatch.setenv("CACHE_TTL_SECONDS", "120")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("APP_HOST", "127.0.0.1")
        monkeypatch.setenv("APP_PORT", "9000")
        monkeypatch.setenv("CORS_ORIGINS", '["https://coach.example.ch"]')
        get_settings.cache_clear()

        settings = get_settings()

        assert settings.COACH_MCP_URL == "http://override.local/mcp"
        assert settings.GITHUB_TOKEN == "ghp_override"  # noqa: S105
        assert settings.GITHUB_REPO == "other/repo"
        assert settings.CACHE_TTL_SECONDS == 120
        assert settings.LOG_LEVEL == "DEBUG"
        assert settings.APP_HOST == "127.0.0.1"
        assert settings.APP_PORT == 9000
        assert settings.CORS_ORIGINS == ["https://coach.example.ch"]

    def test_get_settings_is_cached(self) -> None:
        """get_settings returns the same instance on repeated calls."""
        get_settings.cache_clear()
        first = get_settings()
        second = get_settings()

        assert first is second
        assert isinstance(first, Settings)


class TestDataEncryptionKey:
    """The C2 field-encryption key setting (MADR-0008 C5, #189)."""

    def test_defaults_to_empty_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent env var → empty SecretStr (fail-safe: saves answer 503).

        #243: the absence is pinned explicitly (the shared test environment
        injects a default key for the migration gate — this pin is about the
        Settings default when the variable is genuinely absent).
        """
        monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
        get_settings.cache_clear()

        settings = get_settings()

        assert isinstance(settings.DATA_ENCRYPTION_KEY, SecretStr)
        assert settings.DATA_ENCRYPTION_KEY.get_secret_value() == ""

    def test_env_value_is_loaded_as_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The env value is wrapped as SecretStr — never a plain str attribute."""
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "unit-test-fernet-key-value")
        get_settings.cache_clear()

        settings = get_settings()

        assert isinstance(settings.DATA_ENCRYPTION_KEY, SecretStr)
        assert settings.DATA_ENCRYPTION_KEY.get_secret_value() == "unit-test-fernet-key-value"

    def test_str_of_settings_does_not_expose_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redaction pin (MADR-0008 C5): str()/repr() mask the secret value."""
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "unit-test-fernet-key-value")
        get_settings.cache_clear()

        settings = get_settings()

        assert "unit-test-fernet-key-value" not in str(settings)
        assert "unit-test-fernet-key-value" not in repr(settings)
        assert "**********" in repr(settings.DATA_ENCRYPTION_KEY)
