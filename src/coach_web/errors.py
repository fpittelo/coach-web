"""Owner-safe settings validation reasons — the fixed 422 vocabulary (#181).

The #142 generic-error rule (static "Invalid request payload" detail, no
validation detail echoed) exists to starve the LLM/prompt-injection surface —
not to blind the authenticated owner on their own settings form. Issue #181
splits the posture: the settings endpoints (``PUT /api/objectives``,
``POST /api/periodization/approve``) answer 422 with a structured,
owner-safe body ``{code, message}`` drawn from THIS module's fixed
vocabulary, while every chat/model-facing surface keeps the strict #142
generic detail unchanged.

nLPD constraints baked in here:
- the vocabulary is a closed enum — a reason cannot exist without a message;
- the friendly messages are static strings — no exception text, no input
  echo, no user data (the recipient is the authenticated owner);
- the exception carries the reason as a typed attribute, so the boundary
  maps structured conditions instead of string-matching error messages.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any


class SettingsReason(StrEnum):
    """The fixed vocabulary of owner-safe settings validation reasons (#181)."""

    COVERAGE_TARGET_DATE = "coverage_target_date"
    OVERLAP = "overlap"
    INVALID_DATES = "invalid_dates"
    INVALID_VALUES = "invalid_values"


SETTINGS_REASON_MESSAGES: dict[SettingsReason, str] = {
    SettingsReason.COVERAGE_TARGET_DATE: (
        "Phases must span your objective's target date — extend the last phase to reach it."
    ),
    SettingsReason.OVERLAP: (
        "Phases must not overlap — each phase starts the day after the previous one ends."
    ),
    SettingsReason.INVALID_DATES: "One or more dates are invalid.",
    SettingsReason.INVALID_VALUES: "One or more values are out of range.",
}
"""Reason → friendly static text. Fixed strings only (nLPD: no echo)."""


class SettingsValidationError(ValueError):
    """A settings validation failure carrying its fixed-enum reason (#181).

    Subclasses :class:`ValueError` so every raise site inside a Pydantic
    validator keeps behaving as a validation error (Pydantic wraps it, the
    original instance rides along in the error context), while the boundary
    can read ``.reason`` instead of string-matching messages. The exception
    text stays the technical message for server-side logs; the friendly
    owner-safe text comes exclusively from
    :data:`SETTINGS_REASON_MESSAGES`.
    """

    def __init__(self, reason: SettingsReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def settings_reason_payload(reason: SettingsReason) -> dict[str, str]:
    """Return the owner-safe ``{code, message}`` 422 body for *reason*."""
    return {"code": reason.value, "message": SETTINGS_REASON_MESSAGES[reason]}


def settings_reason_from_errors(errors: Sequence[Mapping[str, Any]]) -> SettingsReason:
    """Extract the threaded reason from FastAPI/Pydantic validation errors.

    Scans the error list for a validator-raised
    :class:`SettingsValidationError` (Pydantic preserves the original
    instance under ``ctx.error``) and returns its reason. Errors without a
    threaded reason — field constraints, literal mismatches, malformed JSON —
    map to the catch-all :attr:`SettingsReason.INVALID_VALUES`. The return
    value is always an enum member, so exception text can never leak into a
    response.
    """
    for error in errors:
        context = error.get("ctx")
        if not isinstance(context, Mapping):
            continue
        original = context.get("error")
        if isinstance(original, SettingsValidationError):
            return original.reason
    return SettingsReason.INVALID_VALUES
