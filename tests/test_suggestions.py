"""Contract tests for the quick-reply suggestion module (MADR-0011, #281).

The suggestion source is SERVER-OWNED CONSTANTS (C-1): the wire payload is
capped by :class:`coach_web.suggestions.SuggestionsPayload` (C-3), the
outcome→suggestions mapping is a single pure choke point keyed on
server-known outcome constants (C-2), the suppression table is
server-authoritative (C-4), and both constant tables are validated at
module load (fail-fast).
"""

import importlib.util
import sys
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

import coach_web.suggestions as suggestions_module
from coach_web.suggestions import (
    DEBRIEF_OUTCOME,
    DEBRIEF_SUGGESTIONS,
    DEFAULT_SUGGESTIONS,
    SUGGESTION_MAX_CHARS,
    SUGGESTION_MAX_ITEMS,
    SUPPRESSING_OUTCOMES,
    SuggestionsPayload,
    TurnOutcome,
    suggestions_for_outcomes,
)


class TestSuggestionsPayloadCaps:
    """The wire contract: 1–4 items, each 1–48 chars after stripping (C-3)."""

    def test_accepts_values_within_the_caps_and_strips_them(self) -> None:
        """Items are stripped before the length caps are evaluated."""
        payload = SuggestionsPayload.model_validate(
            ["  Show this week's plan  ", "x" * SUGGESTION_MAX_CHARS]
        )
        assert payload.root == ["Show this week's plan", "x" * SUGGESTION_MAX_CHARS]

    def test_rejects_more_than_four_items(self) -> None:
        """The list cap is four items (MADR-0011 transport shape)."""
        with pytest.raises(ValidationError):
            SuggestionsPayload.model_validate(["a", "b", "c", "d", "e"])

    def test_rejects_an_empty_list(self) -> None:
        """The list floor is one item; empty means 'no pills' and is never sent."""
        with pytest.raises(ValidationError):
            SuggestionsPayload.model_validate([])

    def test_rejects_an_item_over_the_char_cap(self) -> None:
        """The per-item cap is 48 characters."""
        with pytest.raises(ValidationError):
            SuggestionsPayload.model_validate(["x" * (SUGGESTION_MAX_CHARS + 1)])

    def test_rejects_an_item_that_strips_to_empty(self) -> None:
        """A whitespace-only item strips below the 1-char floor."""
        with pytest.raises(ValidationError):
            SuggestionsPayload.model_validate(["   "])

    def test_rejects_control_characters_inside_an_item(self) -> None:
        """Embedded control characters are rejected (injection surface, C-3)."""
        for item in ("bad\x00item", "line1\nline2", "tab\tinside", "carriage\rreturn"):
            with pytest.raises(ValidationError):
                SuggestionsPayload.model_validate([item])


class TestSuggestionsMapping:
    """The single construction choke point (C-2) and the suppression table (C-4)."""

    def test_a_plain_answer_maps_to_the_default_set(self) -> None:
        """No structured card outcome → the server-owned default set."""
        payload = suggestions_for_outcomes(set())
        assert payload is not None
        assert payload.root == list(DEFAULT_SUGGESTIONS)

    def test_the_debrief_outcome_maps_to_the_debrief_set(self) -> None:
        """A debrief_summary card → the server-owned debrief set."""
        payload = suggestions_for_outcomes({DEBRIEF_OUTCOME})
        assert payload is not None
        assert payload.root == list(DEBRIEF_SUGGESTIONS)

    @pytest.mark.parametrize("outcome", sorted(SUPPRESSING_OUTCOMES))
    def test_each_suppressing_outcome_yields_no_suggestions(self, outcome: TurnOutcome) -> None:
        """Every suppressing card (and the error outcome) suppresses the field."""
        assert suggestions_for_outcomes({outcome}) is None

    def test_co_occurrence_suppression_wins_over_the_debrief_set(self) -> None:
        """C-4 precedence: rpe_request + debrief_summary in one turn → none."""
        assert suggestions_for_outcomes({"rpe_request", DEBRIEF_OUTCOME}) is None

    def test_the_mapping_is_pure_and_returns_a_fresh_copy(self) -> None:
        """Same input → equal output; the input is never mutated; no shared state."""
        outcomes = {DEBRIEF_OUTCOME}
        first = suggestions_for_outcomes(outcomes)
        second = suggestions_for_outcomes(outcomes)
        assert first is not None and second is not None
        assert first.root == second.root
        assert first.root is not second.root
        assert outcomes == {DEBRIEF_OUTCOME}
        first.root.append("drifted")
        third = suggestions_for_outcomes(outcomes)
        assert third is not None
        assert third.root == list(DEBRIEF_SUGGESTIONS)

    def test_unknown_outcomes_degrade_to_the_default_set(self) -> None:
        """C-2: a non-outcome string matches no constant and can never steer
        the mapping beyond the server-owned sets — it degrades to the default.
        The cast documents the defense-in-depth posture: the typed signature
        already keeps free text out; even a leaked non-vocabulary string is
        safe at runtime."""
        leaked = cast("set[TurnOutcome]", {"totally-unrelated-outcome"})
        payload = suggestions_for_outcomes(leaked)
        assert payload is not None
        assert payload.root == list(DEFAULT_SUGGESTIONS)


class TestConstantTablesAreValidatedAtModuleLoad:
    """Fail-fast (C-3): a table violating the wire contract breaks at import."""

    def test_the_shipped_tables_satisfy_the_wire_contract(self) -> None:
        """Both constant sets validate against the emission payload model."""
        for table in (DEFAULT_SUGGESTIONS, DEBRIEF_SUGGESTIONS):
            payload = SuggestionsPayload.model_validate(table)
            assert payload.root == list(table)

    def test_the_shipped_tables_match_the_madr_sets(self) -> None:
        """The initial sets are exactly the MADR-0011 constants (C-1 SSOT)."""
        assert list(DEFAULT_SUGGESTIONS) == [
            "Show this week's plan",
            "How did my last session go?",
            "What should I focus on?",
        ]
        assert list(DEBRIEF_SUGGESTIONS) == [
            "That matches how it felt",
            "Not quite — let's adjust",
            "What's next week's focus?",
        ]

    def test_the_item_cap_constant_matches_the_madr(self) -> None:
        """The caps are the MADR numbers: 1–4 items × 1–48 chars."""
        assert SUGGESTION_MAX_ITEMS == 4
        assert SUGGESTION_MAX_CHARS == 48

    def test_a_tampered_table_breaks_the_module_import(self, tmp_path: Path) -> None:
        """The module-level validation is load-time, not decorative: a table
        edited past the caps fails the import itself (fail-fast)."""
        source = Path(suggestions_module.__file__).read_text(encoding="utf-8")
        anchor = '"What should I focus on?"'
        assert anchor in source, "tamper anchor literal missing from the module source"
        tampered = source.replace(anchor, '"' + "x" * (SUGGESTION_MAX_CHARS + 1) + '"')
        module_path = tmp_path / "suggestions_tampered.py"
        module_path.write_text(tampered, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("suggestions_tampered", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Register before exec (the documented importlib pattern): pydantic's
        # class-creation path resolves the defining module through
        # sys.modules, so an unregistered module breaks model construction
        # before the fail-fast validation can even run.
        sys.modules["suggestions_tampered"] = module
        try:
            with pytest.raises(ValidationError):
                spec.loader.exec_module(module)
        finally:
            sys.modules.pop("suggestions_tampered", None)
