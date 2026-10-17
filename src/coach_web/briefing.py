"""Morning briefing card — pull-model greeting extension (#262, epic #215-A).

On the FIRST page load of the day, when no pending debrief claims the
greeting slot, the coach opens with a server-rendered briefing card:
the readiness band, today's planned session and execution cues.

Security posture (the @cyber-security verdict, APPROVED-WITH-CONTROLS
2026-10-17 — the controls below are this module's contract):

- **C-BR-1:** the health numerics of :class:`ReadinessMetrics`
  (``resting_hr``/``hrv_rmssd``/``sleep_hours``) NEVER leave the server.
  Only the aggregate readiness band — a server-derived closed-enum label —
  travels; ``ctl``/``atl``/``ftp`` are dropped outright (not
  readiness-relevant). This is the #236 precedent (numeric RPE stayed
  in-DB, band labels only) applied to readiness.
- **C-BR-3 (binding construction ruling):** the briefing is a
  server-rendered Pydantic→HTML card emitted as a dedicated SSE event —
  NOT model-streamed prose. Readiness data never enters the LLM prompt,
  so the B8 cross-border exposure is untouched and the card is
  deterministic and sentinel-testable.
- **C-BR-5:** stateless/idempotent — the card is derived per request from
  read-only sources; no table, no write.
- **C-BR-6:** the dashboard read is registered in the ICU GET budget
  (finding F1) and TTL-cached so repeated loads/reconnects do not
  multiply ICU calls.
- **C-BR-9:** stored-content text passes through
  :func:`coach_web.microcycle._digest_single_line` before interpolation.

Band mapping (documented per the spec): the aggregate readiness band is
derived from TSB (Training Stress Balance) alone — the conventional
endurance read of form: ``tsb >= 5`` → ``fresh`` (well-rested),
``-10 <= tsb < 5`` → ``moderate`` (neutral), ``tsb < -10`` → ``fatigued``
(deep negative form). The individual numerics play no part in the label.
"""

import asyncio
import json
import time
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coach_web.config import Settings
from coach_web.mcp_hub import COACH_SERVER
from coach_web.microcycle import _DAY_ROW_PATTERN, _REST_DAY_LOAD, _digest_single_line
from coach_web.models import ReadinessMetrics

ReadinessBand = Literal["fresh", "moderate", "fatigued"]
"""The closed readiness vocabulary (C-BR-2) — server-assembled, never free text."""

FRESH_TSB_THRESHOLD = 5.0
"""TSB at or above this is fresh form (well-rested)."""

FATIGUED_TSB_THRESHOLD = -10.0
"""TSB below this is deep negative form (fatigued)."""

READINESS_TOOL = "intervals_get_readiness_dashboard"
"""Raw coach-mcp tool whose result feeds the readiness band (C-BR-6)."""

READINESS_TOOL_TIMEOUT_SECONDS = 5.0
"""Per-call latency budget for the dashboard read (the #194 mirror pattern).

Bounds the cold-cache read so a slow hub can never stall the greeting:
the whole derivation is additionally bounded by
:data:`BRIEFING_DERIVE_TIMEOUT_SECONDS` at the call site.
"""

BRIEFING_DERIVE_TIMEOUT_SECONDS = 10.0
"""Whole-derivation latency budget (app.py call site, C-BR-7).

A failing or slow ICU read must never delay the stream's first paint:
past this bound the briefing degrades to no event and the turn proceeds.
"""

BAND_CUES: dict[ReadinessBand, str] = {
    "fresh": "Readiness is fresh — hit the planned intensity as written.",
    "moderate": "Readiness is moderate — warm up thoroughly and hold the planned pacing.",
    "fatigued": (
        "Readiness is fatigued — consider trimming the main set and keeping the effort easy."
    ),
}
"""Static server-side execution cues keyed by band (never model-generated)."""

_REST_CELL = "—"
"""The focus-cell marker build_week_markdown renders for 'no focus'."""


def readiness_band(metrics: ReadinessMetrics) -> ReadinessBand:
    """Map the readiness metrics onto the closed band vocabulary (C-BR-2).

    TSB-only mapping (see the module docstring): the individual health
    numerics never influence — never mind become — the label.
    """
    if metrics.tsb >= FRESH_TSB_THRESHOLD:
        return "fresh"
    if metrics.tsb >= FATIGUED_TSB_THRESHOLD:
        return "moderate"
    return "fatigued"


class BriefingSessionBlock(BaseModel):
    """Today's planned session as the briefing card carries it (C-BR-8).

    Workout metadata ONLY — date/title/focus/duration/planned-TSS, the
    same surface the approved-week digest already exposes. No RPE, no
    notes, no wellness values (the C2 fields have no slot on the schema).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: str = Field(..., description="Session date (YYYY-MM-DD).")
    title: str = Field(..., description="Planned workout title (single-line digest).")
    focus: str | None = Field(default=None, description="Focus/zones cell, when present.")
    duration_minutes: float | None = Field(
        default=None, ge=0, description="Planned duration in minutes."
    )
    planned_tss: float | None = Field(default=None, ge=0, description="Planned TSS.")


class MorningBriefingCard(BaseModel):
    """The morning briefing card payload (#262, the #190 card pattern).

    Emitted as the SSE ``morning_briefing`` event data under the
    ``briefing`` key. Purely informational client-side — no actions, no
    editing. The schema is CLOSED (``extra="forbid"``): a readiness
    numeric has no slot to leak into.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    readiness_band: ReadinessBand = Field(
        ..., description="Server-derived aggregate readiness band (C-BR-2)."
    )
    cues: list[str] = Field(
        default_factory=list,
        description="Static server-side execution cues keyed by the band.",
    )
    session: BriefingSessionBlock | None = Field(
        default=None,
        description="Today's planned session, when the approved draft covers today.",
    )


BRIEFING_SESSION_FIELDS = frozenset(BriefingSessionBlock.model_fields)
"""The session block's exact field surface (pinned by the AC8 test)."""


def briefing_session_block(content: str, today: date) -> BriefingSessionBlock | None:
    """Extract today's session block from an approved draft's markdown (AC8).

    The parser understands exactly the day-table shape
    :func:`coach_web.microcycle.build_week_markdown` renders; a rest day
    (the ``—`` load cell) and a date the draft does not cover yield no
    block. Title and focus pass through
    :func:`coach_web.microcycle._digest_single_line` (C-BR-9); the numeric
    cells degrade to ``None`` rather than fabricating a value.
    """
    today_iso = today.isoformat()
    for line in content.splitlines():
        match = _DAY_ROW_PATTERN.match(line.strip())
        if match is None or match.group("date") != today_iso:
            continue
        if match.group("load") == _REST_DAY_LOAD:
            return None
        focus_cell = match.group("focus")
        return BriefingSessionBlock(
            date=today_iso,
            title=_digest_single_line(match.group("title")),
            focus=(None if focus_cell in ("", _REST_CELL) else _digest_single_line(focus_cell)),
            duration_minutes=_parse_cell_number(match.group("duration"), suffix="m"),
            planned_tss=_parse_cell_number(match.group("load")),
        )
    return None


def _parse_cell_number(cell: str, suffix: str = "") -> float | None:
    """Parse a numeric table cell (optionally suffixed), degrading to None."""
    stripped = cell.strip().removesuffix(suffix).strip()
    try:
        return float(stripped)
    except ValueError:
        return None


def build_morning_briefing(
    metrics: ReadinessMetrics,
    content: str | None,
    today: date,
) -> MorningBriefingCard:
    """Build the briefing card from the fetched readiness + the draft (AC3).

    Pure and deterministic: the band is mapped server-side, the cue line
    is a fixed string keyed by the band, and the session block is parsed
    from the stored draft only. ``content`` is ``None`` when no approved
    draft covers the current week — the card then carries the band and
    cues alone.
    """
    band = readiness_band(metrics)
    return MorningBriefingCard(
        readiness_band=band,
        cues=[BAND_CUES[band]],
        session=briefing_session_block(content, today) if content is not None else None,
    )


# ---------------------------------------------------------------------------
# The cached, bounded ICU read (C-BR-6/C-BR-7)
# ---------------------------------------------------------------------------

_READINESS_CACHE: dict[str, tuple[float, ReadinessMetrics]] = {}
"""Single-entry TTL cache for the dashboard read (module-level by design).

Keyed by a fixed key: the coach-mcp sidecar serves exactly one ICU
athlete, so the cached payload has no per-athlete variance. Successes
only — a failed read is never cached (a transient outage must not stick).
"""

_CACHE_KEY = "readiness-dashboard"


def clear_readiness_cache() -> None:
    """Drop the cached readiness metrics (test isolation)."""
    _READINESS_CACHE.clear()


def _parse_readiness(raw: str) -> ReadinessMetrics | None:
    """Parse one hub tool result into :class:`ReadinessMetrics`, or None.

    The hub hands tool results over as JSON strings (and as
    ``{"error": ...}`` on any failure). Every failure mode degrades to
    ``None`` so a failed read can never produce a card (the #190 AC1
    posture: no fabricated numbers).
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or "error" in parsed:
        return None
    try:
        return ReadinessMetrics.model_validate(parsed)
    except ValueError:
        return None


async def fetch_readiness_metrics(
    hub_factory: Any,
    settings: Settings,
    *,
    now: float | None = None,
) -> ReadinessMetrics | None:
    """Fetch the readiness dashboard through the hub, TTL-cached (C-BR-6).

    A cache hit performs NO hub connection and NO ICU call; a miss
    performs exactly one budgeted GET (``ICU_TOOL_GET_COSTS`` entry,
    finding F1) bounded by :data:`READINESS_TOOL_TIMEOUT_SECONDS`. Every
    failure — hub outage, unconnected server, timeout, malformed payload
    — degrades to ``None``; this coroutine never raises (the #194 mirror
    posture). ``now`` is injected so callers (and tests) own the clock.
    """
    moment = time.monotonic() if now is None else now
    cached = _READINESS_CACHE.get(_CACHE_KEY)
    if cached is not None and moment < cached[0]:
        return cached[1]
    hub = None
    try:
        hub = hub_factory(settings)
        await hub.connect()
        if COACH_SERVER not in hub.connected_servers:
            return None
        raw = await asyncio.wait_for(
            hub.execute_tool(READINESS_TOOL),
            timeout=READINESS_TOOL_TIMEOUT_SECONDS,
        )
        metrics = _parse_readiness(raw)
        if metrics is None:
            return None
        _READINESS_CACHE[_CACHE_KEY] = (moment + settings.CACHE_TTL_SECONDS, metrics)
        return metrics
    except Exception:  # noqa: BLE001 - the briefing read must never raise
        return None
    finally:
        if hub is not None:
            await hub.close()


async def derive_morning_briefing(
    hub_factory: Any,
    settings: Settings,
    *,
    content: str | None,
    today: date,
) -> MorningBriefingCard | None:
    """Derive the briefing card, degrading to None on any failure (C-BR-7).

    The never-raises wrapper around the cached read + the pure build: a
    failing ICU read yields no briefing event and never breaks the turn.
    """
    try:
        metrics = await fetch_readiness_metrics(hub_factory, settings)
    except Exception:  # noqa: BLE001 - the derivation must never raise
        return None
    if metrics is None:
        return None
    return build_morning_briefing(metrics, content, today)
