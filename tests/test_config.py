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
        assert settings.STREAMLIT_SERVER_PORT == 8501
        assert settings.STREAMLIT_SERVER_ADDRESS == "0.0.0.0"  # noqa: S104
        assert settings.CACHE_TTL_SECONDS == 60
        assert settings.LOG_LEVEL == "INFO"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables override defaults."""
        monkeypatch.setenv("COACH_MCP_URL", "http://override.local/mcp")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_override")
        monkeypatch.setenv("GITHUB_REPO", "other/repo")
        monkeypatch.setenv("STREAMLIT_SERVER_PORT", "9000")
        monkeypatch.setenv("STREAMLIT_SERVER_ADDRESS", "127.0.0.1")
        monkeypatch.setenv("CACHE_TTL_SECONDS", "120")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        get_settings.cache_clear()

        settings = get_settings()

        assert settings.COACH_MCP_URL == "http://override.local/mcp"
        assert settings.GITHUB_TOKEN == "ghp_override"  # noqa: S105
        assert settings.GITHUB_REPO == "other/repo"
        assert settings.STREAMLIT_SERVER_PORT == 9000
        assert settings.STREAMLIT_SERVER_ADDRESS == "127.0.0.1"
        assert settings.CACHE_TTL_SECONDS == 120
        assert settings.LOG_LEVEL == "DEBUG"

    def test_get_settings_is_cached(self) -> None:
        """get_settings returns the same instance on repeated calls."""
        get_settings.cache_clear()
        first = get_settings()
        second = get_settings()

        assert first is second
        assert isinstance(first, Settings)
