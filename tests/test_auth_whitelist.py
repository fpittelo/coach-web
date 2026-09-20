"""Tests for the email whitelist access-control boundary (issue #65).

The whitelist IS the security boundary: only whitelisted Google accounts may
obtain a session, and every authenticated request is re-checked against it.
"""

from coach_web.auth.whitelist import is_whitelisted, normalize_email

OWNER_EMAIL = "frederic.pitteloud@gmail.com"


class TestNormalizeEmail:
    """Canonical comparison form of an email address."""

    def test_lowercases_and_strips(self) -> None:
        """Normalization lowercases and strips surrounding whitespace."""
        assert normalize_email("  Frederic.Pitteloud@Gmail.COM ") == OWNER_EMAIL

    def test_empty_string_stays_empty(self) -> None:
        """An empty email normalizes to an empty string."""
        assert normalize_email("   ") == ""


class TestIsWhitelisted:
    """Whitelist enforcement semantics (fail closed)."""

    def test_whitelisted_email_is_allowed(self) -> None:
        """The owner email is accepted when present in the whitelist."""
        assert is_whitelisted(OWNER_EMAIL, [OWNER_EMAIL]) is True

    def test_non_whitelisted_email_is_rejected(self) -> None:
        """Any other email is rejected."""
        assert is_whitelisted("attacker@example.com", [OWNER_EMAIL]) is False

    def test_comparison_is_case_insensitive(self) -> None:
        """Google may render the email claim with different casing."""
        assert is_whitelisted("Frederic.Pitteloud@Gmail.Com", [OWNER_EMAIL]) is True
        assert is_whitelisted(OWNER_EMAIL, ["FREDERIC.PITTELOUD@GMAIL.COM"]) is True

    def test_comparison_tolerates_whitespace(self) -> None:
        """Env-provided entries may carry stray whitespace."""
        assert is_whitelisted("  frederic.pitteloud@gmail.com  ", [f" {OWNER_EMAIL} "]) is True

    def test_none_email_is_rejected(self) -> None:
        """A missing email claim never matches."""
        assert is_whitelisted(None, [OWNER_EMAIL]) is False

    def test_empty_email_is_rejected(self) -> None:
        """An empty email claim never matches."""
        assert is_whitelisted("", [OWNER_EMAIL]) is False

    def test_empty_whitelist_rejects_everyone(self) -> None:
        """An empty whitelist fails closed: nobody is allowed."""
        assert is_whitelisted(OWNER_EMAIL, []) is False

    def test_none_whitelist_rejects_everyone(self) -> None:
        """A missing whitelist fails closed: nobody is allowed."""
        assert is_whitelisted(OWNER_EMAIL, None) is False

    def test_multiple_entries(self) -> None:
        """Each whitelist entry grants access independently."""
        whitelist = ["a@b.c", "d@e.f"]
        assert is_whitelisted("d@e.f", whitelist) is True
        assert is_whitelisted("x@y.z", whitelist) is False
