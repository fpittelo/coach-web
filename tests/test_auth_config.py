"""Tests for auth-related settings & fail-closed configuration (issue #65)."""

from typing import Any

import pytest

from coach_web.auth.middleware import AuthConfigError, validate_auth_config
from coach_web.config import Settings, get_settings


class TestAuthSettingsDefaults:
    """Secure, opt-in defaults (local dev stays auth-free)."""

    def test_auth_disabled_by_default(self) -> None:
        """AUTH_ENABLED defaults to false (opt-in)."""
        get_settings.cache_clear()

        assert get_settings().AUTH_ENABLED is False

    def test_default_whitelist_contains_owner(self) -> None:
        """The whitelist defaults to the owner identity (the AC boundary)."""
        get_settings.cache_clear()

        assert get_settings().AUTH_WHITELIST_EMAILS == ["frederic.pitteloud@gmail.com"]

    def test_google_oidc_issuer_default(self) -> None:
        """The issuer default matches the canonical Google OIDC issuer."""
        get_settings.cache_clear()

        assert get_settings().GOOGLE_OIDC_ISSUER == "https://accounts.google.com"

    def test_credential_defaults_are_empty(self) -> None:
        """Credentials default to empty strings (env-only, never hardcoded)."""
        settings = get_settings()

        assert settings.GOOGLE_OIDC_CLIENT_ID == ""
        assert settings.GOOGLE_OIDC_CLIENT_SECRET == ""
        assert settings.AUTH_SESSION_SECRET == ""
        assert settings.AUTH_REDIRECT_URI == ""

    def test_session_ttl_default_is_short_lived(self) -> None:
        """Sessions are short-lived by design (stateless, AC5)."""
        get_settings.cache_clear()

        assert get_settings().AUTH_SESSION_TTL_SECONDS == 3600


class TestAuthSettingsOverrides:
    """Environment overrides (Cloud Run injects the same names, #66)."""

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All auth settings are configurable via environment variables."""
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_ID", "cid.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_SECRET", "secret")
        monkeypatch.setenv("AUTH_SESSION_SECRET", "key")
        monkeypatch.setenv("AUTH_WHITELIST_EMAILS", '["a@b.c", "d@e.f"]')
        monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "1800")
        monkeypatch.setenv("GOOGLE_OIDC_ISSUER", "https://other.issuer")
        monkeypatch.setenv("AUTH_REDIRECT_URI", "https://coach.example.ch/auth/callback")
        get_settings.cache_clear()

        settings = get_settings()

        assert settings.AUTH_ENABLED is True
        assert settings.GOOGLE_OIDC_CLIENT_ID == "cid.apps.googleusercontent.com"
        assert settings.GOOGLE_OIDC_CLIENT_SECRET == "secret"  # noqa: S105
        assert settings.AUTH_SESSION_SECRET == "key"  # noqa: S105
        assert settings.AUTH_WHITELIST_EMAILS == ["a@b.c", "d@e.f"]
        assert settings.AUTH_SESSION_TTL_SECONDS == 1800
        assert settings.GOOGLE_OIDC_ISSUER == "https://other.issuer"
        assert settings.AUTH_REDIRECT_URI == "https://coach.example.ch/auth/callback"


class TestFailClosedValidation:
    """AUTH_ENABLED=true without credentials must fail closed at boot."""

    def _settings(self, **overrides: Any) -> Settings:
        """Return enabled settings with all credentials present by default."""
        values: dict[str, Any] = {
            "AUTH_ENABLED": True,
            "GOOGLE_OIDC_CLIENT_ID": "cid",
            "GOOGLE_OIDC_CLIENT_SECRET": "sec",
            "AUTH_SESSION_SECRET": "unit-test-session-signing-key-0123456789abcdef",
        }
        values.update(overrides)
        return Settings(**values)

    def test_valid_config_passes(self) -> None:
        """A complete auth configuration validates without raising."""
        validate_auth_config(self._settings())

    def test_missing_client_id_raises(self) -> None:
        """Empty GOOGLE_OIDC_CLIENT_ID is rejected."""
        with pytest.raises(AuthConfigError, match="GOOGLE_OIDC_CLIENT_ID"):
            validate_auth_config(self._settings(GOOGLE_OIDC_CLIENT_ID=""))

    def test_missing_client_secret_raises(self) -> None:
        """Empty GOOGLE_OIDC_CLIENT_SECRET is rejected."""
        with pytest.raises(AuthConfigError, match="GOOGLE_OIDC_CLIENT_SECRET"):
            validate_auth_config(self._settings(GOOGLE_OIDC_CLIENT_SECRET=""))

    def test_missing_session_secret_raises(self) -> None:
        """Empty AUTH_SESSION_SECRET is rejected."""
        with pytest.raises(AuthConfigError, match="AUTH_SESSION_SECRET"):
            validate_auth_config(self._settings(AUTH_SESSION_SECRET=""))

    def test_short_session_secret_raises(self) -> None:
        """An HS256 key below 32 bytes (RFC 7518) is rejected."""
        with pytest.raises(AuthConfigError, match="at least 32 bytes"):
            validate_auth_config(self._settings(AUTH_SESSION_SECRET="too-short"))  # noqa: S106

    def test_multibyte_secret_measured_in_bytes(self) -> None:
        """The key-length check counts bytes, matching the error message."""
        # 20 characters but 40 UTF-8 bytes: valid under byte counting.
        validate_auth_config(self._settings(AUTH_SESSION_SECRET="é" * 20))

    def test_all_missing_reports_every_name(self) -> None:
        """The error lists every missing setting at once."""
        with pytest.raises(AuthConfigError) as excinfo:
            validate_auth_config(
                self._settings(
                    GOOGLE_OIDC_CLIENT_ID="",
                    GOOGLE_OIDC_CLIENT_SECRET="",
                    AUTH_SESSION_SECRET="",
                )
            )

        message = str(excinfo.value)
        assert "GOOGLE_OIDC_CLIENT_ID" in message
        assert "GOOGLE_OIDC_CLIENT_SECRET" in message
        assert "AUTH_SESSION_SECRET" in message
