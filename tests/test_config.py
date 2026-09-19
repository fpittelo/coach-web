"""Tests for coach_web.config."""

import pytest

from coach_web.config import Settings, get_settings


class TestSettings:
    """Settings loading and caching behaviour."""

    def test_default_values(self) -> None:
        """Default values are provided when env vars are absent."""
        get_settings.cache_clear()
        settings = get_settings()

        assert settings.COACH_MCP_URL == "http://localhost:8000/mcp"
        assert settings.GITHUB_TOKEN == ""
        assert settings.GITHUB_REPO == "fpittelo/coach"
        assert settings.CACHE_TTL_SECONDS == 60
        assert settings.LOG_LEVEL == "INFO"
        assert settings.APP_HOST == "0.0.0.0"  # noqa: S104
        assert settings.APP_PORT == 8080
        assert settings.CORS_ORIGINS == ["http://localhost:8080"]

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
