"""Debrief ICU session-comment mirror — the post-save write path (#190).

Story 3.2 completes PO decision 1a (#163): a saved debrief is mirrored to
Intervals.icu as a session comment through the coach-mcp
``icu_add_session_comment`` tool. The mirror is NON-BLOCKING on the debrief
save: the debrief row is committed BEFORE the mirror starts, and every
mirror failure degrades to a fixed-vocabulary soft warning — the debrief is
never touched by mirror failures (AC3).

The comment is SERVER-GENERATED from the validated debrief data only
(RPE + short notes excerpt) in a fixed format, sanitized with the upstream
semantics (script/style blocks and HTML tags stripped, control characters
removed) and hard-capped below the upstream 2000-character limit — the tool
is never a raw-LLM passthrough channel (upstream security condition 4).

The ICU activity is resolved by date from the bounded completed-sessions
read: zero matches, several matches, and read failures each map to their
own fixed reason — nothing is ever posted to a guessed activity.

#194 hardening: every mirror tool call is bounded by the mirror-specific
latency budget (``MIRROR_TOOL_TIMEOUT_SECONDS`` — well under the hub's
2×30 s worst case), and the upstream post reply is classified into the
LOCAL three-state outcome (``posted`` / ``failed`` / ``unknown``) — an
unconfirmable reply is never treated as mirror-confirmed.
"""

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from coach_web.analysis import parse_completed_sessions
from coach_web.config import Settings
from coach_web.debrief import DebriefMirrorOutcome
from coach_web.errors import MIRROR_REASON_MESSAGES, MirrorReason
from coach_web.mcp_hub import COACH_SERVER, MCPHubError

COMPLETED_SESSIONS_TOOL = "icu_get_completed_sessions"
"""Raw coach-mcp tool resolving the ICU activity for the debriefed date."""

ADD_COMMENT_TOOL = "icu_add_session_comment"
"""Raw coach-mcp tool posting the mirrored debrief comment."""

MIRROR_COMMENT_MAX_LENGTH = 2000
"""Hard cap matching the upstream ``SESSION_COMMENT_MAX_LENGTH`` (#82 condition 4)."""

MIRROR_NOTES_EXCERPT_LENGTH = 500
"""Short notes excerpt carried by the mirrored comment (RPE + short text)."""

MIRROR_SUCCESS_MESSAGE = "Debrief mirrored to Intervals.icu."
"""Fixed friendly text for a successful mirror."""

MIRROR_NOTES_PLACEHOLDER = "(none submitted)"
"""Fixed placeholder when the debrief carried no notes."""

MIRROR_TOOL_TIMEOUT_SECONDS = 10.0
"""Per-tool-call latency budget for the mirror (#194 AC2).

The mirror runs inline on the debrief save response path (non-blocking on
the DURABILITY, not on the response): its two sequential hub tool calls
(resolve + post) would otherwise inherit the hub's 30 s per-call timeout —
up to ~2×30 s on a slow hub. Each mirror tool call is bounded by THIS
strictly smaller budget (two-call worst case 2×10 s), and a timed-out call
degrades to the fixed soft warning — the mirror never raises.
"""

MIRROR_POSTED_PREFIX = "Successfully posted session comment"
"""Upstream success reply prefix — the documented string contract.

Upstream contract (coach ``src/coach_mcp/server.py``
``icu_add_session_comment``, verified 2026-10-11 @ 7a5dcb7): the tool
returns a plain string — ``"Successfully posted session comment on
activity '<id>': …"`` on success, ``"Error …"`` on failure. This prefix
match IS the success signal; a structured upstream success signal would be
a coach-repo contract change and is deferred (fpittelo/coach#82 follow-up,
#194 AC3).
"""

_UPSTREAM_ERROR_PREFIX = "Error"
"""Upstream failure reply prefix — both upstream failure shapes start with it."""


class MirrorPostVerdict(StrEnum):
    """The three-state classification of the upstream post reply (#194 AC3).

    ``posted`` — the documented success prefix AND the resolved activity id
    appear in the reply (mirror-confirmed). ``failed`` — the upstream Error
    shape (a definite application failure). ``unknown`` — anything else
    (contract drift, hub transport errors, a success-shaped reply about a
    different activity): NEVER treated as mirror-confirmed; surfaced
    honestly as the fixed ``post_unconfirmed`` soft warning.
    """

    POSTED = "posted"
    FAILED = "failed"
    UNKNOWN = "unknown"


_SCRIPT_BLOCK_RE = re.compile(
    r"<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>",
    re.IGNORECASE | re.DOTALL,
)
"""Complete script/style blocks (content included) — upstream sanitize semantics."""

_HTML_TAG_RE = re.compile(r"<[^>]*>")
"""Any remaining HTML/XML tag (drops inline event-handler vectors)."""

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
"""C0 control characters (except \\n and \\t) and DEL."""

_HORIZONTAL_WS_RE = re.compile(r"[ \t]+")
"""Runs of horizontal whitespace collapsed to a single space."""


def _sanitize_comment_text(text: str) -> str:
    """Sanitize free text destined for the upstream comment field.

    Mirrors the upstream ``sanitize_comment_text`` semantics (epic #82
    binding security condition 4): complete ``<script>``/``<style>`` blocks
    (content included) are removed, any remaining HTML/XML tag is removed,
    control characters (except newlines and tabs) and DEL are removed, runs
    of spaces/tabs collapse to a single space, and leading/trailing
    whitespace is stripped. Newlines are preserved for multi-line notes.
    """
    without_blocks = _SCRIPT_BLOCK_RE.sub(" ", text)
    without_tags = _HTML_TAG_RE.sub(" ", without_blocks)
    without_controls = _CONTROL_CHARS_RE.sub("", without_tags)
    collapsed = _HORIZONTAL_WS_RE.sub(" ", without_controls)
    return collapsed.strip()


def build_mirror_comment(*, plan_session_key: str, rpe: int, notes: str | None) -> str:
    """Build the fixed-format mirror comment from validated debrief data only.

    The comment carries the session date, the RPE and a short sanitized
    notes excerpt — never a raw-LLM passthrough (upstream security
    condition 4). The result is hard-capped at the upstream 2000-character
    limit and can never be empty (the header and RPE line are structural).
    """
    if notes:
        excerpt = _sanitize_comment_text(notes)[:MIRROR_NOTES_EXCERPT_LENGTH]
    else:
        excerpt = MIRROR_NOTES_PLACEHOLDER
    if not excerpt:
        excerpt = MIRROR_NOTES_PLACEHOLDER
    comment = (
        f"Coach Web session debrief — {plan_session_key}\n" f"RPE: {rpe}/10\n" f"Notes: {excerpt}"
    )
    return comment[:MIRROR_COMMENT_MAX_LENGTH]


def _failed_outcome(reason: MirrorReason) -> DebriefMirrorOutcome:
    """Build the fixed soft-warning outcome for *reason*."""
    return DebriefMirrorOutcome(
        status="failed", code=reason, message=MIRROR_REASON_MESSAGES[reason]
    )


@dataclass
class _Resolved:
    """A unique activity match — the mirror may proceed."""

    activity_id: str


@dataclass
class _ResolutionFailure:
    """No postable activity — the fixed reason why."""

    reason: MirrorReason


async def _resolve_activity_id(
    hub: Any,
    plan_session_key: str,
) -> _Resolved | _ResolutionFailure:
    """Resolve the ICU activity id for the debriefed session date.

    The bounded completed-sessions read is pinned to the single session day
    (one GET); the date match must be unique — zero matches, several
    matches and read failures each map to their own fixed reason, so
    nothing is ever posted to a guessed activity. The read is bounded by
    the mirror latency budget (#194 AC2).
    """
    try:
        result = await asyncio.wait_for(
            hub.execute_tool(
                COMPLETED_SESSIONS_TOOL,
                {"oldest": plan_session_key, "newest": plan_session_key},
            ),
            timeout=MIRROR_TOOL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # A timed-out read is a definite non-answer — the ICU side is
        # unavailable (the same fixed reason as an error-shaped read).
        return _ResolutionFailure(MirrorReason.HUB_UNAVAILABLE)
    page = parse_completed_sessions(result)
    if page is None:
        # A failed or error-shaped read cannot resolve an activity — the
        # ICU side is unavailable, not absent.
        return _ResolutionFailure(MirrorReason.HUB_UNAVAILABLE)
    matches = [session for session in page.sessions if session.date == plan_session_key]
    if not matches:
        return _ResolutionFailure(MirrorReason.SESSION_NOT_FOUND)
    if len(matches) > 1:
        return _ResolutionFailure(MirrorReason.SESSION_AMBIGUOUS)
    return _Resolved(matches[0].id)


def _classify_post_reply(result: str, activity_id: str) -> MirrorPostVerdict:
    """Classify the upstream post reply into the three-state verdict (#194 AC3).

    The upstream ``icu_add_session_comment`` tool returns a plain string —
    ``"Successfully posted session comment on activity '<id>': …"`` on
    success, ``"Error …"`` on failure (coach ``server.py`` ~L690, verified
    2026-10-11 @ 7a5dcb7). This string match IS the success signal: a
    structured upstream success signal would be a coach-repo contract
    change and is deferred (fpittelo/coach#82). ``posted`` additionally
    requires the RESOLVED activity id in the reply, so a success-shaped
    reply about a different activity can never confirm this mirror.
    """
    if result.startswith(MIRROR_POSTED_PREFIX) and activity_id in result:
        return MirrorPostVerdict.POSTED
    if result.startswith(_UPSTREAM_ERROR_PREFIX):
        return MirrorPostVerdict.FAILED
    return MirrorPostVerdict.UNKNOWN


async def mirror_debrief_to_icu(
    hub_factory: Any,
    settings: Settings,
    *,
    plan_session_key: str,
    rpe: int,
    notes: str | None,
) -> tuple[DebriefMirrorOutcome, str | None]:
    """Mirror one saved debrief to Intervals.icu as a session comment.

    The mirror is NON-BLOCKING on the debrief save: this coroutine never
    raises — every failure degrades to the fixed-vocabulary soft warning
    and the debrief row (already durable) is never touched. Each tool call
    is bounded by ``MIRROR_TOOL_TIMEOUT_SECONDS`` (#194 AC2). The returned
    activity id (successful resolution) lets the caller link the debrief
    row to its ICU mirror.

    Returns:
        ``(outcome, activity_id)`` — the outcome carries ``mirrored`` or
        the fixed failure vocabulary; the activity id is the resolved ICU
        activity on success, ``None`` otherwise.
    """
    hub = hub_factory(settings)
    try:
        try:
            await hub.connect()
        except MCPHubError:
            return _failed_outcome(MirrorReason.HUB_UNAVAILABLE), None
        if COACH_SERVER not in hub.connected_servers:
            return _failed_outcome(MirrorReason.HUB_UNAVAILABLE), None
        resolution = await _resolve_activity_id(hub, plan_session_key)
        if isinstance(resolution, _ResolutionFailure):
            return _failed_outcome(resolution.reason), None
        comment = build_mirror_comment(plan_session_key=plan_session_key, rpe=rpe, notes=notes)
        try:
            result = await asyncio.wait_for(
                hub.execute_tool(
                    ADD_COMMENT_TOOL,
                    {
                        "activity_id": resolution.activity_id,
                        "comment": comment,
                        "confirmed": True,
                    },
                ),
                timeout=MIRROR_TOOL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # A timed-out WRITE is genuinely ambiguous — the comment may have
            # landed; claiming a definite failure would be dishonest (#194).
            return _failed_outcome(MirrorReason.POST_UNCONFIRMED), None
        verdict = _classify_post_reply(result, resolution.activity_id)
        if verdict is MirrorPostVerdict.POSTED:
            return (
                DebriefMirrorOutcome(status="mirrored", code=None, message=MIRROR_SUCCESS_MESSAGE),
                resolution.activity_id,
            )
        if verdict is MirrorPostVerdict.FAILED:
            return _failed_outcome(MirrorReason.POST_FAILED), None
        # UNKNOWN: an unconfirmable reply is never mirror-confirmed — it
        # surfaces the honest post_unconfirmed soft warning (#194 AC3).
        return _failed_outcome(MirrorReason.POST_UNCONFIRMED), None
    except Exception:  # noqa: BLE001 - the mirror must never raise (AC3)
        return _failed_outcome(MirrorReason.POST_FAILED), None
    finally:
        await hub.close()
