"""Settings reason enum contract — the owner-safe 422 vocabulary (#181).

The #142 generic-error rule is over-applied to owner-facing settings forms:
a 422 whose only payload is "Invalid request payload" hides the actual
validation reason from the one person allowed to see it (the authenticated
owner — the #142 threat model is the LLM/prompt-injection surface, not the
owner's own settings form). Issue #181 introduces a FIXED reason vocabulary:
the settings endpoints answer 422 with ``{code, message}`` where ``code``
comes from :class:`~coach_web.errors.SettingsReason` and ``message`` from the
fixed map — never exception text, never an input echo (Swiss nLPD).

This module is the single source of truth for that vocabulary and is
contract-tested here: the enum membership, the exact friendly strings, the
exception threading and the RequestValidationError extraction are all pinned,
so a new reason cannot silently ship without a message and the chat-facing
#142 posture stays untouched.
"""

from typing import Any

import pytest

from coach_web.errors import (
    ENCRYPTION_REASON_MESSAGES,
    SETTINGS_REASON_MESSAGES,
    EncryptionReason,
    EncryptionUnavailableError,
    SettingsReason,
    SettingsValidationError,
    encryption_reason_payload,
    settings_reason_from_errors,
    settings_reason_payload,
)


class TestSettingsReasonEnum:
    """The fixed vocabulary — exactly four codes, exactly four messages."""

    def test_enum_has_exactly_the_four_documented_codes(self) -> None:
        """The vocabulary is closed: no code can be added without a contract change."""
        assert {reason.value for reason in SettingsReason} == {
            "coverage_target_date",
            "overlap",
            "invalid_dates",
            "invalid_values",
        }

    def test_every_code_maps_to_its_exact_friendly_message(self) -> None:
        """The messages are the documented fixed strings — static, owner-safe."""
        assert (
            SETTINGS_REASON_MESSAGES[SettingsReason.COVERAGE_TARGET_DATE]
            == "Phases must span your objective's target date — extend the last phase to reach it."
        )
        assert (
            SETTINGS_REASON_MESSAGES[SettingsReason.OVERLAP]
            == "Phases must not overlap — each phase starts the day after the previous one ends."
        )
        assert SETTINGS_REASON_MESSAGES[SettingsReason.INVALID_DATES] == (
            "One or more dates are invalid."
        )
        assert SETTINGS_REASON_MESSAGES[SettingsReason.INVALID_VALUES] == (
            "One or more values are out of range."
        )

    def test_message_map_covers_the_whole_enum(self) -> None:
        """Every reason has exactly one message — no missing, no extra entries."""
        assert set(SETTINGS_REASON_MESSAGES) == set(SettingsReason)

    def test_payload_carries_code_and_message_only(self) -> None:
        """The 422 body shape is exactly {code, message} — nothing else leaks."""
        payload = settings_reason_payload(SettingsReason.OVERLAP)

        assert payload == {
            "code": "overlap",
            "message": "Phases must not overlap — each phase starts the day after the "
            "previous one ends.",
        }
        assert set(payload) == {"code", "message"}


class TestSettingsValidationError:
    """The threading exception — a ValueError carrying its reason (#181)."""

    def test_is_a_value_error_so_pydantic_wraps_it(self) -> None:
        """Validators raising it stay validation errors (Pydantic wraps ValueError)."""
        error = SettingsValidationError(SettingsReason.OVERLAP, "technical text")

        assert isinstance(error, ValueError)

    def test_carries_its_reason_attribute(self) -> None:
        """The reason rides the exception — no string-matching at the boundary."""
        error = SettingsValidationError(SettingsReason.COVERAGE_TARGET_DATE, "technical text")

        assert error.reason is SettingsReason.COVERAGE_TARGET_DATE

    def test_technical_message_stays_server_side(self) -> None:
        """The exception text is the technical log message, not the friendly one."""
        error = SettingsValidationError(
            SettingsReason.INVALID_DATES, "date '09/2027' is not a valid calendar date"
        )

        assert str(error) == "date '09/2027' is not a valid calendar date"
        # The friendly message never mixes into the exception text.
        assert "dates are invalid" not in str(error)


class TestReasonExtraction:
    """RequestValidationError → reason extraction (threaded, not string-matched)."""

    def test_unattributed_errors_default_to_invalid_values(self) -> None:
        """Boundary errors without a threaded reason map to the catch-all code."""
        errors: list[dict[str, Any]] = [
            {"type": "greater_than_equal", "loc": ("n",), "msg": ">= 0"},
            {"type": "literal_error", "loc": ("t",), "msg": "not allowed"},
        ]

        assert settings_reason_from_errors(errors) is SettingsReason.INVALID_VALUES

    def test_empty_errors_default_to_invalid_values(self) -> None:
        """No errors at all (e.g. a JSON decode failure) still yield a reason."""
        assert settings_reason_from_errors([]) is SettingsReason.INVALID_VALUES

    def test_threaded_reason_is_extracted_from_the_error_context(self) -> None:
        """A validator-raised SettingsValidationError rides ctx.error into the handler."""
        threaded: dict[str, Any] = {
            "type": "value_error",
            "loc": ("plan", "phases"),
            "msg": "phases must be monotonic and non-overlapping",
            "ctx": {
                "error": SettingsValidationError(
                    SettingsReason.OVERLAP, "phases must be monotonic and non-overlapping"
                )
            },
        }

        assert settings_reason_from_errors([threaded]) is SettingsReason.OVERLAP

    def test_first_threaded_reason_wins(self) -> None:
        """The earliest threaded reason is reported; unattributed errors never mask it."""
        threaded: dict[str, Any] = {
            "type": "value_error",
            "loc": ("d",),
            "msg": "bad date",
            "ctx": {
                "error": SettingsValidationError(
                    SettingsReason.INVALID_DATES, "date must match ISO date pattern"
                )
            },
        }

        assert (
            settings_reason_from_errors([threaded, {"type": "missing", "loc": ("x",)}])
            is SettingsReason.INVALID_DATES
        )

    def test_malformed_context_entries_are_ignored(self) -> None:
        """A ctx without an exception instance never crashes the extraction."""
        errors: list[dict[str, Any]] = [
            {"type": "value_error", "loc": ("x",), "msg": "boom", "ctx": {"error": "not-an-exc"}},
            {"type": "value_error", "loc": ("y",), "msg": "boom", "ctx": None},
        ]

        assert settings_reason_from_errors(errors) is SettingsReason.INVALID_VALUES

    def test_extraction_never_returns_the_exception_text(self) -> None:
        """The extracted value is always an enum member — exception text cannot leak."""
        weird: dict[str, Any] = {
            "type": "value_error",
            "loc": (),
            "msg": "anything",
            "ctx": {"error": SettingsValidationError(SettingsReason.INVALID_VALUES, "secret")},
        }

        assert settings_reason_from_errors([weird]) in set(SettingsReason)


@pytest.mark.parametrize("reason", list(SettingsReason))
def test_every_reason_payload_is_owner_safe(reason: SettingsReason) -> None:
    """No friendly message echoes input-shaped content (quotes, braces, backslashes)."""
    payload = settings_reason_payload(reason)

    assert payload["code"] == reason.value
    assert payload["message"] == SETTINGS_REASON_MESSAGES[reason]
    for forbidden in ("{", "}", "\\", "'"):
        # The apostrophe appears only inside the possessive "objective's" — the
        # check below excludes that exact documented occurrence.
        if forbidden == "'" and reason is SettingsReason.COVERAGE_TARGET_DATE:
            continue
        assert forbidden not in payload["message"]


class TestEncryptionReasonEnum:
    """The fixed 503 vocabulary for encryption-unavailable debrief saves (#189).

    Story 3.1 splits a second owner-facing error posture out of the #142
    generic rule: when the C2 data-encryption key is missing or invalid, the
    debrief save answers 503 with ``{code, message}`` from THIS closed
    vocabulary — never exception text, never key material, and never a
    plaintext fallback. The pattern mirrors :class:`SettingsReason` (#181)
    but is a separate enum: the settings 422 contract test above pins
    SettingsReason's membership exactly, so the encryption vocabulary cannot
    silently ride the same enum.
    """

    def test_enum_has_exactly_the_documented_code(self) -> None:
        """The vocabulary is closed: no code can be added without a contract change."""
        assert {reason.value for reason in EncryptionReason} == {"key_unavailable"}

    def test_code_maps_to_its_exact_friendly_message(self) -> None:
        """The message is the documented fixed string — static, owner-safe."""
        assert ENCRYPTION_REASON_MESSAGES[EncryptionReason.KEY_UNAVAILABLE] == (
            "Encrypted storage is unavailable — the debrief was not saved. "
            "Check the DATA_ENCRYPTION_KEY configuration."
        )

    def test_message_map_covers_the_whole_enum(self) -> None:
        """Every reason has exactly one message — no missing, no extra entries."""
        assert set(ENCRYPTION_REASON_MESSAGES) == set(EncryptionReason)

    def test_payload_carries_code_and_message_only(self) -> None:
        """The 503 body shape is exactly {code, message} — nothing else leaks."""
        payload = encryption_reason_payload(EncryptionReason.KEY_UNAVAILABLE)

        assert payload == {
            "code": "key_unavailable",
            "message": (
                "Encrypted storage is unavailable — the debrief was not saved. "
                "Check the DATA_ENCRYPTION_KEY configuration."
            ),
        }
        assert set(payload) == {"code", "message"}

    def test_payload_never_carries_key_material(self) -> None:
        """No friendly message can echo the key value — the map is static text."""
        payload = encryption_reason_payload(EncryptionReason.KEY_UNAVAILABLE)

        assert "gAAAA" not in payload["message"]
        assert "base64" not in payload["message"]


class TestEncryptionUnavailableError:
    """The threading exception for encryption-unavailable saves (#189)."""

    def test_is_a_runtime_error(self) -> None:
        """It is a hard service condition, not a request-validation error."""
        error = EncryptionUnavailableError(EncryptionReason.KEY_UNAVAILABLE, "technical text")

        assert isinstance(error, RuntimeError)

    def test_carries_its_reason_attribute(self) -> None:
        """The reason rides the exception — no string-matching at the boundary."""
        error = EncryptionUnavailableError(EncryptionReason.KEY_UNAVAILABLE, "technical text")

        assert error.reason is EncryptionReason.KEY_UNAVAILABLE

    def test_technical_message_stays_server_side(self) -> None:
        """The exception text is the technical log message, not the friendly one."""
        error = EncryptionUnavailableError(
            EncryptionReason.KEY_UNAVAILABLE,
            "DATA_ENCRYPTION_KEY is not a valid Fernet key: bad base64",
        )

        assert "bad base64" in str(error)
        # The friendly message never mixes into the exception text.
        assert "Encrypted storage is unavailable" not in str(error)
