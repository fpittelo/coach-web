"""Server-owned quick-reply suggestion sets (MADR-0011, #281).

The quick-reply pills beneath completed coach messages are fed by an
**optional** ``suggestions`` field on the terminal ``done`` SSE event. The
source is SERVER-OWNED CONSTANTS — never model output (C-1: any change to
the source re-triggers MADR-0008 security review). This module is the
single home of that source:

- :class:`SuggestionsPayload` — the Pydantic wire contract (1–4 items,
  each 1–48 chars after stripping, control-character-free), enforced at
  the emission site on every ``done`` carrying the field (C-3).
- :func:`suggestions_for_outcomes` — the SINGLE construction choke point
  (C-2): a pure function mapping server-known turn-outcome constants to a
  validated copy of the constant sets. No parameter accepts free text.
- :func:`outcome_from_event` — the sole admission path from a typed SSE
  event category to a mapping key (C-2: a model- or tool-derived string
  can never become a key).

Suppression is server-authoritative, per-turn and monotonic (C-4): the
cards that carry their own explicit action affordances (``plan_proposal``,
``week_plan``, ``periodization_plan``, ``rpe_request``,
``morning_briefing``) suppress the field entirely, as does any ``error``
outcome — the ``done`` events that follow errors carry no suggestions.
"""

from collections.abc import Iterable
from typing import Annotated, Final, Literal
from unicodedata import category

from pydantic import Field, RootModel, StringConstraints, field_validator

SUGGESTION_MAX_ITEMS: Final[int] = 4
"""Wire cap on the number of pills per turn (MADR-0011 transport shape)."""

SUGGESTION_MAX_CHARS: Final[int] = 48
"""Wire cap on one pill label, evaluated after stripping."""

TurnOutcome = Literal[
    "plan_proposal",
    "week_plan",
    "periodization_plan",
    "rpe_request",
    "debrief_summary",
    "morning_briefing",
    "error",
]
"""The server-known turn-outcome vocabulary (C-2).

The suggestion-relevant subset of the typed ``AgentEventType`` vocabulary.
The mapping key is NEVER model text: a tool name, card title or any
model/tool-derived string is not part of this closed set.
"""

_OUTCOME_BY_EVENT: Final[dict[str, TurnOutcome]] = {
    "plan_proposal": "plan_proposal",
    "week_plan": "week_plan",
    "periodization_plan": "periodization_plan",
    "rpe_request": "rpe_request",
    "debrief_summary": "debrief_summary",
    "morning_briefing": "morning_briefing",
    "error": "error",
}
"""The fixed event-category → outcome admission table (C-2).

The ONLY path from a streamed event to a mapping key: membership here is
the sole admission, so the agent loop cannot accidentally feed a
model-derived string into the mapping.
"""


def outcome_from_event(event_type: str) -> TurnOutcome | None:
    """Narrow a typed SSE event category to its turn outcome, or ``None``.

    Events outside the outcome vocabulary (``token``, ``status``, …) carry
    no suggestion relevance and map to ``None``.
    """
    return _OUTCOME_BY_EVENT.get(event_type)


SUPPRESSING_OUTCOMES: Final[frozenset[TurnOutcome]] = frozenset(
    {
        "plan_proposal",
        "week_plan",
        "periodization_plan",
        "rpe_request",
        "morning_briefing",
        "error",
    }
)
"""Outcomes that suppress the ``suggestions`` field entirely (C-4).

The five cards carry their own explicit action affordances — pills would
duplicate them (``morning_briefing`` SUPPRESSES: there is no briefing
set). ``error`` covers the table's error row: the ``done`` events that
follow errors carry no suggestions, and a mid-turn error suppresses too.
"""

DEBRIEF_OUTCOME: Final[TurnOutcome] = "debrief_summary"
"""The one non-suppressing card outcome: it maps to the debrief set."""

SuggestionItem = Annotated[
    str,
    StringConstraints(min_length=1, max_length=SUGGESTION_MAX_CHARS, strip_whitespace=True),
]
"""One pill label: 1–48 chars after stripping (MADR-0011 transport shape)."""

SuggestionsList = Annotated[
    list[SuggestionItem],
    Field(min_length=1, max_length=SUGGESTION_MAX_ITEMS),
]
"""The pill list: 1–4 items (MADR-0011 transport shape)."""


class SuggestionsPayload(RootModel[SuggestionsList]):
    """Validated ``suggestions`` wire payload (MADR-0011 C-3).

    A root list of 1–4 pill labels, each 1–48 chars after stripping and
    free of control characters. Enforced at the emission site on every
    ``done`` that carries the field — the ``done`` payload never inherits
    ``suggestions`` from a model-derived dict.
    """

    @field_validator("root")
    @classmethod
    def _reject_control_characters(cls, value: list[str]) -> list[str]:
        """Reject any label carrying a control character after stripping."""
        for item in value:
            if any(category(char) == "Cc" for char in item):
                raise ValueError("suggestion labels must be control-character-free")
        return value


DEFAULT_SUGGESTIONS: Final[tuple[str, ...]] = (
    "Show this week's plan",
    "How did my last session go?",
    "What should I focus on?",
)
"""Pills after a plain conversational answer (MADR-0011 initial set)."""

DEBRIEF_SUGGESTIONS: Final[tuple[str, ...]] = (
    "That matches how it felt",
    "Not quite — let's adjust",
    "What's next week's focus?",
)
"""Pills after a ``debrief_summary`` card (MADR-0011 initial set)."""

# Fail-fast (C-3): the constant tables must satisfy the same wire contract
# they are emitted under — a bad edit breaks at import, never in production.
SuggestionsPayload.model_validate(DEFAULT_SUGGESTIONS)
SuggestionsPayload.model_validate(DEBRIEF_SUGGESTIONS)


def suggestions_for_outcomes(outcomes: Iterable[TurnOutcome]) -> SuggestionsPayload | None:
    """Map one turn's observed outcome constants to its pill set (C-2).

    The SINGLE construction point for the ``suggestions`` value: a pure
    function of the server-known outcome constants observed during the turn
    to a freshly validated copy of the constant sets. Suppression is
    server-authoritative and monotonic (C-4): ANY suppressing outcome in
    the set suppresses the field entirely — including co-occurrence with a
    non-suppressing outcome (``rpe_request`` + ``debrief_summary`` → none).

    Returns ``None`` when the turn must carry no ``suggestions`` key.
    """
    observed = frozenset(outcomes)
    if observed & SUPPRESSING_OUTCOMES:
        return None
    if DEBRIEF_OUTCOME in observed:
        return SuggestionsPayload.model_validate(DEBRIEF_SUGGESTIONS)
    return SuggestionsPayload.model_validate(DEFAULT_SUGGESTIONS)
