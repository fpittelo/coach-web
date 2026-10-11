"""Contract tests for the planned-vs-executed analysis layer (#190).

Story 3.2 wires the ICU compliance tools into the coach flow: the agent's
``icu_get_workout_compliance`` / ``icu_get_completed_sessions`` results are
parsed into strict mirrors of the upstream coach-mcp whitelist shapes, the
per-user-action ICU GET budget is enforced, and the debrief summary card is
built SERVER-SIDE from the fetched data only — never from agent-claimed
numbers (AC1: every claim traceable to a fetched session, no fabricated
numbers).

The contract pinned here:

- the mirrors accept exactly the upstream JSON shapes (extra keys tolerated
  for forward compatibility, missing/malformed fields rejected) and every
  parse failure yields ``None`` — a failed or error-shaped tool result can
  never produce a card (no fabricated data);
- the GET budget counts the documented per-tool costs (compliance = 2,
  completed/wellness/trends = 1) against the ≤4/user-action cap (PO decision
  5a) and refuses — without executing — any call that would exceed it;
- the summary card joins compliance events with completed sessions by date,
  carries the executed load when the sessions page is known, keeps the
  planned side unclaimed (the whitelisted upstream contract exposes no
  planned load — the card never fabricates one), assigns severity
  deterministically (missed → critical, structure mismatch → warn, else ok),
  and renders a fixed-format headline derived from the report numbers alone.
"""

from coach_web.analysis import (
    ICU_GET_BUDGET,
    ICU_TOOL_GET_COSTS,
    CompletedSessionMirror,
    ComplianceEventMirror,
    DebriefSummaryCard,
    IcuReadBudget,
    SessionDebriefRow,
    build_debrief_summary_card,
    icu_tool_get_cost,
    parse_completed_sessions,
    parse_compliance_report,
)

# ---------------------------------------------------------------------------
# Upstream-shaped fixtures (mirrors of the coach-mcp whitelist payloads)
# ---------------------------------------------------------------------------


def _compliance_event(**overrides: object) -> dict[str, object]:
    """Return one upstream-shaped compliance event payload."""
    payload: dict[str, object] = {
        "event_id": "401",
        "date": "2026-10-06",
        "name": "Threshold 3x12",
        "status": "completed",
        "compliance": 87.5,
        "structure_diff": "match",
    }
    payload.update(overrides)
    return payload


def _compliance_report(**overrides: object) -> dict[str, object]:
    """Return one upstream-shaped compliance report payload."""
    payload: dict[str, object] = {
        "oldest": "2026-10-05",
        "newest": "2026-10-11",
        "total_planned": 2,
        "completed": 1,
        "missed": 1,
        "upcoming": 0,
        "adherence_rate": 50.0,
        "events": [
            _compliance_event(),
            _compliance_event(
                event_id="402",
                date="2026-10-08",
                name="Easy spin",
                status="missed",
                compliance=None,
                structure_diff="unavailable",
            ),
        ],
    }
    payload.update(overrides)
    return payload


def _completed_session(**overrides: object) -> dict[str, object]:
    """Return one upstream-shaped completed-session summary payload."""
    payload: dict[str, object] = {
        "id": "12345678",
        "date": "2026-10-06",
        "type": "Ride",
        "title": "Threshold 3x12",
        "duration_seconds": 4500.0,
        "load": 91.0,
    }
    payload.update(overrides)
    return payload


def _completed_page(**overrides: object) -> dict[str, object]:
    """Return one upstream-shaped completed-sessions page payload."""
    payload: dict[str, object] = {
        "oldest": "2026-10-05",
        "newest": "2026-10-11",
        "activity_type": None,
        "fetched": 1,
        "returned": 1,
        "sessions": [_completed_session()],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Compliance report mirror
# ---------------------------------------------------------------------------


class TestComplianceReportMirror:
    """The strict mirror of the upstream WorkoutComplianceReport JSON."""

    def test_valid_report_parses_every_field(self) -> None:
        """The whitelisted fields survive the parse unchanged."""
        report = parse_compliance_report(_json(_compliance_report()))

        assert report is not None
        assert report.oldest == "2026-10-05"
        assert report.newest == "2026-10-11"
        assert report.total_planned == 2
        assert report.completed == 1
        assert report.missed == 1
        assert report.upcoming == 0
        assert report.adherence_rate == 50.0
        assert len(report.events) == 2
        first = report.events[0]
        assert isinstance(first, ComplianceEventMirror)
        assert first.event_id == "401"
        assert first.status == "completed"
        assert first.compliance == 87.5
        assert first.structure_diff == "match"

    def test_report_without_adherence_rate_parses(self) -> None:
        """A nothing-rateable window carries adherence_rate=None upstream."""
        payload = _compliance_report(
            total_planned=0, completed=0, missed=0, upcoming=2, adherence_rate=None, events=[]
        )

        report = parse_compliance_report(_json(payload))

        assert report is not None
        assert report.adherence_rate is None
        assert report.events == []

    def test_error_payload_yields_none(self) -> None:
        """A hub error JSON can never become a report (no fabricated data)."""
        assert parse_compliance_report('{"error": "Tool timed out"}') is None

    def test_malformed_json_yields_none(self) -> None:
        """Junk output degrades to no report — never an exception."""
        assert parse_compliance_report("not json at all") is None

    def test_non_object_json_yields_none(self) -> None:
        """A JSON array or scalar is not a report."""
        assert parse_compliance_report("[1, 2, 3]") is None
        assert parse_compliance_report("42") is None

    def test_missing_required_field_yields_none(self) -> None:
        """A report missing a whitelisted field is rejected wholesale."""
        payload = _compliance_report()
        del payload["missed"]

        assert parse_compliance_report(_json(payload)) is None

    def test_invalid_status_value_yields_none(self) -> None:
        """The status classification is a closed literal — drift is rejected."""
        payload = _compliance_report()
        payload["events"] = [_compliance_event(status="skipped")]

        assert parse_compliance_report(_json(payload)) is None

    def test_extra_upstream_fields_are_tolerated(self) -> None:
        """Forward-compatible: unknown upstream additions never break the parse."""
        payload = _compliance_report(future_field="whatever")

        assert parse_compliance_report(_json(payload)) is not None


# ---------------------------------------------------------------------------
# Completed sessions mirror
# ---------------------------------------------------------------------------


class TestCompletedSessionsMirror:
    """The strict mirror of the upstream CompletedSessionsPage JSON."""

    def test_valid_page_parses_every_field(self) -> None:
        """The whitelisted session fields survive the parse unchanged."""
        page = parse_completed_sessions(_json(_completed_page()))

        assert page is not None
        assert page.fetched == 1
        assert page.returned == 1
        assert page.activity_type is None
        assert len(page.sessions) == 1
        session = page.sessions[0]
        assert isinstance(session, CompletedSessionMirror)
        assert session.id == "12345678"
        assert session.date == "2026-10-06"
        assert session.type == "Ride"
        assert session.title == "Threshold 3x12"
        assert session.duration_seconds == 4500.0
        assert session.load == 91.0

    def test_session_without_load_parses(self) -> None:
        """Load is optional upstream (activities without TSS exist)."""
        payload = _completed_page()
        payload["sessions"] = [_completed_session(load=None)]

        page = parse_completed_sessions(_json(payload))

        assert page is not None
        assert page.sessions[0].load is None

    def test_error_payload_yields_none(self) -> None:
        """A hub error JSON can never become a page."""
        assert parse_completed_sessions('{"error": "rate limited"}') is None

    def test_malformed_json_yields_none(self) -> None:
        """Junk output degrades to no page."""
        assert parse_completed_sessions("{{{") is None

    def test_missing_required_field_yields_none(self) -> None:
        """A page missing a whitelisted field is rejected wholesale."""
        payload = _completed_page()
        del payload["returned"]

        assert parse_completed_sessions(_json(payload)) is None


# ---------------------------------------------------------------------------
# ICU GET budget (PO decision 5a)
# ---------------------------------------------------------------------------


class TestIcuGetBudget:
    """The ≤4 GETs/user-action budget over the documented per-tool costs."""

    def test_budget_is_four(self) -> None:
        """PO decision 5a: at most four ICU GETs per user action."""
        assert ICU_GET_BUDGET == 4

    def test_documented_tool_costs(self) -> None:
        """The cost table matches the upstream contracts exactly."""
        assert ICU_TOOL_GET_COSTS == {
            "icu_get_workout_compliance": 2,
            "icu_get_completed_sessions": 1,
            "icu_get_wellness": 1,
            "icu_get_trends": 1,
        }

    def test_cost_lookup_strips_the_hub_namespace(self) -> None:
        """Namespaced hub tool names resolve to their raw cost entry."""
        assert icu_tool_get_cost("coach-mcp__icu_get_workout_compliance") == 2
        assert icu_tool_get_cost("icu_get_completed_sessions") == 1

    def test_non_icu_tools_cost_nothing(self) -> None:
        """GitHub tools and synthetic tools are outside the ICU budget."""
        assert icu_tool_get_cost("github-mcp__create_issue") is None
        assert icu_tool_get_cost("propose_plan") is None

    def test_compliance_plus_completed_fits_the_budget(self) -> None:
        """The documented analysis flow (2 + 1 = 3 GETs) is within budget."""
        budget = IcuReadBudget()

        assert not budget.would_exceed(2)
        budget.consume(2)
        assert not budget.would_exceed(1)
        budget.consume(1)

        assert budget.used == 3

    def test_call_exceeding_the_budget_is_refused(self) -> None:
        """A call that would push the total past 4 is refused before executing."""
        budget = IcuReadBudget()
        budget.consume(2)
        budget.consume(1)

        assert budget.would_exceed(2) is True

    def test_refused_call_does_not_consume(self) -> None:
        """Refusal is a decision, not a charge — the counter stays put."""
        budget = IcuReadBudget()
        budget.consume(3)

        assert budget.would_exceed(2) is True
        assert budget.used == 3

    def test_exactly_the_budget_is_allowed(self) -> None:
        """Reaching exactly 4 GETs is the boundary — still allowed."""
        budget = IcuReadBudget()
        budget.consume(2)
        budget.consume(1)

        assert budget.would_exceed(1) is False
        budget.consume(1)

        assert budget.used == 4
        assert budget.would_exceed(1) is True


# ---------------------------------------------------------------------------
# Debrief summary card (the #165 contract, adapted to the weekly flow)
# ---------------------------------------------------------------------------


class TestBuildDebriefSummaryCard:
    """The server-built card — joined, delta-computed, severity-assigned."""

    def test_card_joins_compliance_with_completed_sessions(self) -> None:
        """A completed event gains its executed load from the sessions page."""
        report = parse_compliance_report(_json(_compliance_report()))
        page = parse_completed_sessions(_json(_completed_page()))

        card = build_debrief_summary_card(report, page)  # type: ignore[arg-type]

        assert isinstance(card, DebriefSummaryCard)
        assert card.oldest == "2026-10-05"
        assert card.newest == "2026-10-11"
        assert card.total_planned == 2
        assert card.completed == 1
        assert card.missed == 1
        assert card.upcoming == 0
        assert card.adherence_rate == 50.0
        assert len(card.sessions) == 2

    def test_completed_row_joins_executed_load_only(self) -> None:
        """Executed load joins by date; the planned side stays unclaimed.

        The whitelisted upstream compliance contract exposes no planned load,
        so ``planned_tss``/``load_delta_pct`` stay None in this flow — the
        card never fabricates a planned number (AC1). The per-session
        planned-vs-executed read is ICU's own compliance score plus the
        structure verdict.
        """
        report = parse_compliance_report(_json(_compliance_report()))
        page = parse_completed_sessions(_json(_completed_page()))

        card = build_debrief_summary_card(report, page)  # type: ignore[arg-type]

        row = card.sessions[0]
        assert isinstance(row, SessionDebriefRow)
        assert row.session_date == "2026-10-06"
        assert row.session_title == "Threshold 3x12"
        assert row.status == "completed"
        assert row.compliance_pct == 87.5
        assert row.structure_diff == "match"
        assert row.planned_tss is None
        assert row.executed_tss == 91.0
        assert row.load_delta_pct is None
        assert row.severity == "ok"

    def test_missed_row_is_critical_without_executed_data(self) -> None:
        """A missed session has nothing executed — severity is critical."""
        report = parse_compliance_report(_json(_compliance_report()))
        card = build_debrief_summary_card(report, None)  # type: ignore[arg-type]

        row = card.sessions[1]

        assert row.status == "missed"
        assert row.executed_tss is None
        assert row.severity == "critical"

    def test_structure_mismatch_is_warn(self) -> None:
        """A completed session whose executed structure diverges is a warning."""
        payload = _compliance_report()
        payload["events"] = [_compliance_event(structure_diff="mismatch")]
        report = parse_compliance_report(_json(payload))

        card = build_debrief_summary_card(report, None)  # type: ignore[arg-type]

        assert card.sessions[0].severity == "warn"

    def test_completed_without_compliance_is_ok_when_structure_matches(self) -> None:
        """No compliance score and a matching structure is not a warning."""
        payload = _compliance_report()
        payload["events"] = [_compliance_event(compliance=None)]
        report = parse_compliance_report(_json(payload))

        card = build_debrief_summary_card(report, None)  # type: ignore[arg-type]

        assert card.sessions[0].severity == "ok"

    def test_upcoming_row_is_ok_and_carries_no_executed_data(self) -> None:
        """Future events are informational — never penalized."""
        payload = _compliance_report(
            total_planned=1,
            completed=0,
            missed=0,
            upcoming=1,
            adherence_rate=None,
            events=[_compliance_event(status="upcoming", compliance=None)],
        )
        report = parse_compliance_report(_json(payload))

        card = build_debrief_summary_card(report, None)  # type: ignore[arg-type]

        row = card.sessions[0]
        assert row.status == "upcoming"
        assert row.compliance_pct is None
        assert row.executed_tss is None
        assert row.severity == "ok"

    def test_headline_is_the_fixed_adherence_sentence(self) -> None:
        """The plain-language read is derived from the report numbers alone."""
        report = parse_compliance_report(_json(_compliance_report()))

        card = build_debrief_summary_card(report, None)  # type: ignore[arg-type]

        assert card.headline == (
            "You planned 2 workout(s) and completed 1 — 50.0% adherence (1 missed)."
        )

    def test_headline_without_rateable_workouts(self) -> None:
        """An all-upcoming window renders the nothing-rateable sentence."""
        payload = _compliance_report(
            total_planned=2,
            completed=0,
            missed=0,
            upcoming=2,
            adherence_rate=None,
            events=[],
        )
        report = parse_compliance_report(_json(payload))

        card = build_debrief_summary_card(report, None)  # type: ignore[arg-type]

        assert card.headline == "2 planned workout(s) upcoming — nothing rateable yet."

    def test_sessions_are_ordered_by_date(self) -> None:
        """The card reads chronologically regardless of upstream order."""
        payload = _compliance_report(
            events=[
                _compliance_event(event_id="402", date="2026-10-08", name="Easy spin"),
                _compliance_event(event_id="401", date="2026-10-06", name="Threshold 3x12"),
            ]
        )
        report = parse_compliance_report(_json(payload))

        card = build_debrief_summary_card(report, None)  # type: ignore[arg-type]

        assert [row.session_date for row in card.sessions] == ["2026-10-06", "2026-10-08"]

    def test_card_is_serializable_for_the_sse_payload(self) -> None:
        """The card round-trips through model_dump for the SSE event data."""
        report = parse_compliance_report(_json(_compliance_report()))
        page = parse_completed_sessions(_json(_completed_page()))

        card = build_debrief_summary_card(report, page)  # type: ignore[arg-type]
        payload = card.model_dump()

        assert payload["sessions"][0]["session_date"] == "2026-10-06"
        assert payload["sessions"][0]["severity"] == "ok"
        assert payload["headline"].startswith("You planned")


def _json(payload: object) -> str:
    """Serialise a fixture payload the way the hub hands tool results over."""
    import json

    return json.dumps(payload)
