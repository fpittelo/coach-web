"""Post-session debrief — C2-encrypted capture & pending derivation (#189).

Story 3.1 (epic #161) completes the capture half of the debrief loop: after a
completed planned session, the athlete submits an RPE (1–10) with optional
free-text notes, and the debrief persists in the ``debriefs`` table (MADR-008
class C2 — SENSITIVE health data, nLPD Art. 5(c)) with the notes as Fernet
ciphertext. The analysis half (planned-vs-executed + ICU mirror) is Story 3.2;
this module is deliberately ICU-free.

Encryption posture (MADR-008 X3 + security conditions C5/C6): the Fernet key
travels in ``DATA_ENCRYPTION_KEY`` (per-lane secret, loaded as
``pydantic.SecretStr``). A missing or invalid key raises
:class:`~coach_web.errors.EncryptionUnavailableError`, which the API boundary
maps to the fixed 503 ``{code, message}`` vocabulary — there is NO plaintext
fallback code path: the notes are encrypted here or not persisted at all.

Proactivity posture (MADR-0010): the pending derivation is a pure function of
(approved plan drafts, today, the athlete's debrief rows) — completed
(strictly past) non-rest day slots of approved drafts without a debrief row
for THIS athlete. The coach digest injects the pending list (metadata only:
date + title) into the system prompt so the coach opens the debrief flow with
the ``request_rpe`` tool; the pending-debrief banner renders from the same
derivation via ``GET /api/debrief/pending``.

Per-athlete isolation (#189 AC4): every query is scoped by ``athlete_id`` —
derived from the authenticated session email (auth-enabled lanes) or the
single local athlete default (auth-disabled dev/qa lanes). Athlete B cannot
read or write athlete A's debriefs.

Weekly adherence metric (#234, epic #213 S4): the debrief-save path is also
the metric's event-driven write trigger — :func:`upsert_week_adherence`
recomputes the ISO week of the debriefed session from the existing DB
compliance data (approved drafts' day rows vs the athlete's debriefs) and
upserts the C3 ``week_adherence`` row (COUNTS AND PERCENTAGES ONLY — no
health content; the pure computation lives in :mod:`coach_web.analysis`).
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import CheckConstraint, Float, Integer, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from coach_web.analysis import WeekAdherenceMetric, compute_week_adherence, iso_week_key
from coach_web.auth.whitelist import normalize_email
from coach_web.config import Settings
from coach_web.db import Base
from coach_web.errors import (
    EncryptionReason,
    EncryptionUnavailableError,
    MirrorReason,
)
from coach_web.microcycle import (
    MAX_SESSION_TITLE_LENGTH,
    PlanDraftRow,
    _digest_single_line,
    week_bounds,
)
from coach_web.models import validate_iso_date

DEFAULT_ATHLETE_ID = 1
"""Athlete id for auth-disabled lanes (dev/qa): the single local athlete."""

MAX_DEBRIEF_NOTES_LENGTH = 2000
"""Cap on the optional free-text debrief notes (prompt-injection surface bound)."""

PENDING_DEBRIEF_DIGEST_HEADER = (
    "Pending session debriefs (the athlete completed these planned sessions "
    "without a debrief — greet the athlete by opening the check-in for the "
    "most recent one with the request_rpe tool, asking how the session went):"
)
"""System-prompt header for the pending-debrief digest (MADR-0010 greeting hook)."""

_REST_DAY_LOAD = "—"
"""The load-column marker build_week_markdown renders for rest days."""

_DAY_ROW_PATTERN = re.compile(
    r"^\|\s*(?:mon|tue|wed|thu|fri|sat|sun)\s*"
    r"\|\s*(?P<date>\d{4}-\d{2}-\d{2})\s*"
    r"\|\s*(?P<title>.+?)\s*"
    r"\|\s*(?P<focus>.*?)\s*"
    r"\|\s*(?P<load>.*?)\s*"
    r"\|\s*(?P<duration>.*?)\s*\|\s*$"
)
"""Day-table row shape rendered by :func:`coach_web.microcycle.build_week_markdown`."""


class DebriefSaveRequest(BaseModel):
    """Payload for ``POST /api/debrief`` (#189 AC1).

    One structured submission per planned session: the RPE is a strict
    integer 1–10 and the optional notes are length-capped. The notes are
    encrypted at the persistence boundary — they exist in plaintext only
    inside the request handling process, never at rest and never in a
    response, log or SSE payload (nLPD).

    ``mirror_to_icu`` is the athlete's explicit, per-save consent to mirror
    the debrief to Intervals.icu as a session comment (#190 AC3): strictly
    opt-in (default ``False`` — the plain save never touches ICU), and the
    mirror itself is non-blocking on the save (the debrief is durable before
    the mirror starts).
    """

    plan_session_key: str = Field(
        ...,
        description="Planned-session key (the session's ISO date, YYYY-MM-DD)",
    )
    rpe: int = Field(
        ...,
        ge=1,
        le=10,
        strict=True,
        description="Session RPE, 1 (effortless) to 10 (maximal effort)",
    )
    notes: str | None = Field(
        default=None,
        max_length=MAX_DEBRIEF_NOTES_LENGTH,
        description="Optional free-text session notes (stored Fernet-encrypted)",
    )
    mirror_to_icu: bool = Field(
        default=False,
        description=(
            "Athlete's explicit per-save consent to mirror this debrief to "
            "Intervals.icu as a session comment (#190 AC3) — strictly opt-in."
        ),
    )

    @field_validator("plan_session_key")
    @classmethod
    def _validate_plan_session_key(cls, value: str) -> str:
        """Validate the key as a real ISO calendar date (shared rule)."""
        return validate_iso_date(value) or value


class DebriefSaveResponse(BaseModel):
    """Response payload for ``POST /api/debrief`` (201).

    Metadata only — the notes are never echoed back (nLPD posture: the
    athlete just wrote them; the confirmation carries no copy). The optional
    ``mirror`` outcome rides along when the athlete asked for the ICU
    session-comment mirror (#190 AC3): ``None`` when no mirror was
    requested, so the pre-#190 body shape is byte-identical on that path.
    The optional ``week_adherence`` (#234 AC3) rides along when the
    debriefed session's ISO week has planned sessions — the metric computed
    and upserted on this same save path; ``None`` (excluded from the body)
    keeps the pre-#234 shape byte-identical when the week has no planned
    sessions or the best-effort upsert degraded.
    """

    status: Literal["saved"] = Field(..., description="Fixed confirmation status")
    plan_session_key: str = Field(..., description="The debriefed session key")
    rpe: int = Field(..., ge=1, le=10, description="The stored RPE")
    mirror: "DebriefMirrorOutcome | None" = Field(
        default=None,
        description=(
            "ICU session-comment mirror outcome (#190 AC3) — present only when "
            "the athlete asked for the mirror; None keeps the body shape "
            "byte-identical to the pre-#190 contract."
        ),
    )
    week_adherence: WeekAdherenceMetric | None = Field(
        default=None,
        description=(
            "The debriefed session's ISO-week plan adherence (#234) — counts "
            "and percentage only (C3); None when the week has no planned "
            "sessions (no metric row) or the best-effort upsert degraded."
        ),
    )


class DebriefMirrorOutcome(BaseModel):
    """The debrief ICU session-comment mirror outcome (#190 AC3).

    ``status`` is ``mirrored`` when the comment was posted upstream and
    ``failed`` otherwise; ``code`` carries the fixed-vocabulary reason on
    failure (``None`` when mirrored); ``message`` is the fixed friendly
    text — always leading with the saved-confirmation so a mirror failure
    can never read as a lost debrief.
    """

    status: Literal["mirrored", "failed"]
    code: MirrorReason | None = None
    message: str


class PendingDebrief(BaseModel):
    """One completed planned session awaiting this athlete's debrief (#189 AC3).

    Metadata only (MADR-0010 privacy posture): date and title — never
    telemetry, never another athlete's debrief content.
    """

    plan_session_key: str = Field(..., description="The session's ISO date (YYYY-MM-DD)")
    session_title: str = Field(..., description="Planned session title from the draft")


class PendingDebriefResponse(BaseModel):
    """Response payload for ``GET /api/debrief/pending`` (#189 AC3)."""

    pending: list[PendingDebrief] = Field(
        default_factory=list,
        description="Completed plan sessions without a debrief row, oldest first",
    )


class RpeRequest(BaseModel):
    """The coach's ``request_rpe`` tool payload (SSE ``rpe_request`` event).

    The coach opens the debrief flow for one session (MADR-0010); the UI
    renders the RPE stepper card from this payload. Strictly validated
    server-side — never trust agent-emitted values.
    """

    plan_session_key: str = Field(
        ...,
        description="Planned-session key (the session's ISO date, YYYY-MM-DD)",
    )
    session_title: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SESSION_TITLE_LENGTH,
        description="Session title shown on the check-in card",
    )

    @field_validator("plan_session_key")
    @classmethod
    def _validate_plan_session_key(cls, value: str) -> str:
        """Validate the key as a real ISO calendar date (shared rule)."""
        return validate_iso_date(value) or value


class DebriefRow(Base):
    """ORM model for the ``debriefs`` table (MADR-008 C2 — encrypted notes)."""

    __tablename__ = "debriefs"
    # The RPE CHECK below is the ORM mirror of migration 0004's CHECK (both
    # hardcoded deliberately — migrations stay self-contained, never
    # importing application symbols); keep the two in sync.
    __table_args__ = (CheckConstraint("rpe BETWEEN 1 AND 10", name="ck_debriefs_rpe_range"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    session_external_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_session_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    rpe: Mapped[int] = mapped_column(Integer, nullable=False)
    notes_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class WeekAdherenceRow(Base):
    """ORM model for the ``week_adherence`` table (#234, MADR-008 C3).

    One ISO week's plan-adherence metric per athlete — COUNTS AND
    PERCENTAGES ONLY: no RPE, no notes, no wellness values ever join this
    row (the C3 classification stays honest; health content remains in the
    C2 ``debriefs`` table).
    """

    __tablename__ = "week_adherence"
    # The percentage CHECK below is the ORM mirror of migration 0005's CHECK
    # (both hardcoded deliberately — migrations stay self-contained, never
    # importing application symbols); keep the two in sync.
    __table_args__ = (
        CheckConstraint("adherence_pct BETWEEN 0 AND 100", name="ck_week_adherence_pct_range"),
        UniqueConstraint("athlete_id", "week_key", name="uq_week_adherence_athlete_week"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False)
    week_key: Mapped[str] = mapped_column(Text, nullable=False)
    planned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    adherence_pct: Mapped[float] = mapped_column(Float, nullable=False)
    computed_at: Mapped[str] = mapped_column(Text, nullable=False)


def athlete_id_from_email(email: str | None) -> int:
    """Resolve the request's athlete id from the session email (#189 AC4).

    Auth-disabled lanes (no session email) map to the single local athlete.
    Auth-enabled requests derive a stable positive id from the canonical
    email form — a pseudonymization that also keeps the email out of the
    database. Different emails map to different ids, so every debrief query
    scoped by ``athlete_id`` is isolated per athlete.
    """
    if not email:
        return DEFAULT_ATHLETE_ID
    digest = hashlib.sha256(normalize_email(email).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def build_debrief_cipher(settings: Settings) -> Fernet:
    """Build the Fernet cipher from ``DATA_ENCRYPTION_KEY`` (#189 AC2).

    Raises:
        EncryptionUnavailableError: when the key is missing (empty) or not a
            valid Fernet key. The technical message names the env var, never
            its value; the boundary maps the typed reason to the fixed 503
            vocabulary. There is no plaintext fallback.
    """
    key = settings.DATA_ENCRYPTION_KEY.get_secret_value()
    if not key:
        raise EncryptionUnavailableError(
            EncryptionReason.KEY_UNAVAILABLE, "DATA_ENCRYPTION_KEY is not configured"
        )
    try:
        return Fernet(key)
    except ValueError as exc:
        raise EncryptionUnavailableError(
            EncryptionReason.KEY_UNAVAILABLE, "DATA_ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (MADR-008 convention)."""
    return datetime.now(UTC).isoformat()


async def save_debrief(
    session: AsyncSession,
    *,
    athlete_id: int,
    payload: DebriefSaveRequest,
    cipher: Fernet,
) -> DebriefRow:
    """Persist one debrief (upsert per athlete + session key) and commit.

    The notes are encrypted HERE — the plaintext never reaches the SQL layer,
    so no database file (main or WAL) can ever contain it (AC1). A
    re-submission for the same athlete + session key updates the stored RPE
    and notes (idempotent retries; one debrief per session per athlete).
    """
    result = await session.execute(
        select(DebriefRow).where(
            DebriefRow.athlete_id == athlete_id,
            DebriefRow.plan_session_key == payload.plan_session_key,
        )
    )
    row = result.scalar_one_or_none()
    notes_ciphertext = (
        cipher.encrypt(payload.notes.encode("utf-8")).decode("ascii")
        if payload.notes is not None
        else None
    )
    now = _utc_now_iso()
    if row is None:
        row = DebriefRow(
            athlete_id=athlete_id,
            session_external_id=None,
            plan_session_key=payload.plan_session_key,
            rpe=payload.rpe,
            notes_encrypted=notes_ciphertext,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.rpe = payload.rpe
        row.notes_encrypted = notes_ciphertext
        row.updated_at = now
    await session.commit()
    return row


def parse_plan_day_rows(content: str) -> list[PendingDebrief]:
    """Extract the non-rest day slots from an approved draft's markdown (#189 AC3).

    The parser understands exactly the day-table shape
    :func:`coach_web.microcycle.build_week_markdown` renders; any other line
    (prose, foreign tables, malformed rows) fails safe to no session. Rest
    days carry no session to rate and never become pending. The plan-session
    key IS the day's ISO date — the local, stable identity of a planned
    session (``session_external_id`` stays reserved for the deferred ICU
    workout-link story: Story 3.2's mirror deliberately drops the resolved
    activity id — #194 AC1).
    """
    sessions: list[PendingDebrief] = []
    for line in content.splitlines():
        match = _DAY_ROW_PATTERN.match(line.strip())
        if match is None:
            continue
        if match.group("load") == _REST_DAY_LOAD:
            continue
        day_date = match.group("date")
        try:
            date.fromisoformat(day_date)
        except ValueError:
            continue
        sessions.append(
            PendingDebrief(plan_session_key=day_date, session_title=match.group("title"))
        )
    return sessions


async def list_pending_debriefs(
    session: AsyncSession,
    *,
    athlete_id: int,
    today: date,
) -> list[PendingDebrief]:
    """Derive the athlete's pending debriefs (#189 AC3, MADR-0010).

    Pending = day slots of APPROVED plan drafts strictly before ``today``
    (the local proxy for "completed" — the ICU completion marking arrives
    with Story 3.2) that have no debrief row for THIS athlete. Ordered
    oldest first; ``today`` is injected so callers own the clock boundary.
    """
    result = await session.execute(
        select(PlanDraftRow).where(PlanDraftRow.status == "approved").order_by(PlanDraftRow.id)
    )
    candidates: dict[str, PendingDebrief] = {}
    for row in result.scalars().all():
        for candidate in parse_plan_day_rows(row.content):
            candidates[candidate.plan_session_key] = candidate
    completed = [
        candidate for key, candidate in candidates.items() if date.fromisoformat(key) < today
    ]
    if not completed:
        return []
    debriefed = await session.execute(
        select(DebriefRow.plan_session_key).where(DebriefRow.athlete_id == athlete_id)
    )
    done_keys = {key for (key,) in debriefed.all()}
    pending = [candidate for candidate in completed if candidate.plan_session_key not in done_keys]
    pending.sort(key=lambda item: item.plan_session_key)
    return pending


async def upsert_week_adherence(
    session: AsyncSession,
    *,
    athlete_id: int,
    plan_session_key: str,
) -> WeekAdherenceMetric | None:
    """Recompute and persist the debriefed session's ISO-week adherence (#234).

    Event-driven on the debrief-save path (no background jobs, no new
    endpoints): the ISO week of ``plan_session_key`` is recomputed from the
    source rows — planned sessions from APPROVED plan drafts' day rows,
    completed sessions from THIS athlete's debrief rows — and upserted into
    ``week_adherence`` (counts/percentages only, C3). Idempotent per
    (athlete, week): a re-save updates the row, never duplicates it. A week
    with zero planned sessions writes no row (0/0 noise avoided) and the
    metric stays unclaimed.
    """
    week_key = iso_week_key(date.fromisoformat(plan_session_key))
    monday, sunday = week_bounds(week_key)
    drafts = await session.execute(select(PlanDraftRow).where(PlanDraftRow.status == "approved"))
    planned = {
        item.plan_session_key
        for draft in drafts.scalars().all()
        for item in parse_plan_day_rows(draft.content)
        if monday <= item.plan_session_key <= sunday
    }
    debriefed = await session.execute(
        select(DebriefRow.plan_session_key).where(DebriefRow.athlete_id == athlete_id)
    )
    metric = compute_week_adherence(week_key, planned, (key for (key,) in debriefed.all()))
    if metric is None:
        return None
    row = (
        await session.execute(
            select(WeekAdherenceRow).where(
                WeekAdherenceRow.athlete_id == athlete_id,
                WeekAdherenceRow.week_key == week_key,
            )
        )
    ).scalar_one_or_none()
    now = _utc_now_iso()
    if row is None:
        session.add(
            WeekAdherenceRow(
                athlete_id=athlete_id,
                week_key=metric.week_key,
                planned_count=metric.planned_count,
                completed_count=metric.completed_count,
                adherence_pct=metric.adherence_pct,
                computed_at=now,
            )
        )
    else:
        row.planned_count = metric.planned_count
        row.completed_count = metric.completed_count
        row.adherence_pct = metric.adherence_pct
        row.computed_at = now
    await session.commit()
    return metric


def pending_debrief_digest(pending: Sequence[PendingDebrief]) -> str | None:
    """Build the token-budgeted pending-debrief digest for the coach prompt.

    Metadata only — session keys and titles, never notes or RPE (nLPD; the
    notes are not even decrypted on this path). ``None`` when nothing is
    pending (graceful empty state: no digest section at all).
    """
    if not pending:
        return None
    lines = [f"- {item.plan_session_key}: {item.session_title}" for item in pending]
    return "\n".join([PENDING_DEBRIEF_DIGEST_HEADER, *lines])


def apply_pending_debrief_digest(
    system_prompt: str,
    pending: Sequence[PendingDebrief],
) -> str:
    """Append the pending-debrief digest to a base system prompt (MADR-0010).

    A no-op when nothing is pending — the prompt stays byte-identical, so
    the greeting hook can never degrade an unrelated turn.
    """
    digest = pending_debrief_digest(pending)
    if digest is None:
        return system_prompt
    return f"{system_prompt}\n\n{digest}"


# ---------------------------------------------------------------------------
# Divergence digest (#236, epic #213 S2) — plan-vs-reality context
# ---------------------------------------------------------------------------


DIVERGENCE_DIGEST_MAX_CHARS = 600
"""Hard character budget for the divergence digest (~150 tokens, C5)."""

DIVERGENCE_DIGEST_HEADER = (
    "Divergence digest for the approved week (plan vs executed — read-only "
    "facts; ground any mid-week adjustment the athlete requests in these):"
)
"""System-prompt header for the divergence digest (#236 AC1).

The digest is READ-ONLY context (MADR-0010 pull model, ADR-0010): it
proposes nothing itself — the coach uses the facts when the athlete asks
for a mid-week adjustment.
"""

SeverityBand = Literal["low", "moderate", "high"]
"""The fixed-vocabulary severity-chip labels (C-S2-3): a closed enum, never
free text. Mapped server-side from the stored RPE — the numeric value never
leaves the database (C-S2-1)."""


def rpe_severity_band(rpe: int) -> SeverityBand:
    """Map a stored RPE to its fixed-vocabulary severity band (C-S2-1).

    The mapping is total over the RPE domain (the ``debriefs`` CHECK bounds
    it to 1-10) and runs SERVER-SIDE: only the band label is ever emitted —
    the numeric RPE value stays in the database. Bands: 1-3 low, 4-6
    moderate, 7-10 high (7+ is the classic flagged-exertion range).
    """
    if rpe <= 3:
        return "low"
    if rpe <= 6:
        return "moderate"
    return "high"


class FlaggedExertionFact(BaseModel):
    """One flagged-exertion divergence fact (#236 AC1/AC4).

    Metadata + severity chip only: the session's ISO date, the planned
    title (collapsed to one safe line at build time, C-S2-7) and the
    server-mapped RPE severity band label. The numeric RPE value has no
    slot in the fact (C-S2-1) and no note text can ever reach it (C-S2-2).
    """

    plan_session_key: str = Field(..., description="The session's ISO date (YYYY-MM-DD).")
    session_title: str = Field(
        ...,
        description="Planned session title (single-line collapsed at build time).",
    )
    severity: SeverityBand = Field(
        ...,
        description="Server-mapped RPE severity band label (the severity chip).",
    )


class DivergenceDigest(BaseModel):
    """Plan-vs-reality divergence digest for the approved week (#236 AC1).

    A pure function of DB rows (the approved draft's day slots + this
    athlete's debrief rows) and the request-scoped ``today`` — the agent
    cannot influence the facts. Counts and severity chips only: no numeric
    RPE, no note text, no wellness values (C-S2-1/C-S2-2).
    """

    missed_count: int = Field(
        ...,
        ge=0,
        strict=True,
        description=(
            "Planned sessions so far (strictly before today) without a "
            "debrief row for this athlete."
        ),
    )
    flagged_count: int = Field(
        ...,
        ge=0,
        strict=True,
        description="Debriefed sessions whose RPE maps to the high severity band.",
    )
    flagged_sessions: list[FlaggedExertionFact] = Field(
        default_factory=list,
        description="Per-day flagged-exertion facts, chronological.",
    )

    @model_validator(mode="after")
    def _check_digest_invariants(self) -> "DivergenceDigest":
        """Counts are server-verified, never trusted (#165 posture).

        ``flagged_count`` must equal the fact list's length, and a digest
        with zero divergence is never constructed — the builder returns
        ``None`` for that case (no noise section in the prompt).
        """
        if self.flagged_count != len(self.flagged_sessions):
            raise ValueError("flagged_count must equal the number of flagged sessions")
        if self.missed_count == 0 and self.flagged_count == 0:
            raise ValueError("a divergence digest requires at least one divergence fact")
        return self


async def list_debrief_rpes(session: AsyncSession, *, athlete_id: int) -> dict[str, int]:
    """Map this athlete's debrief session keys to their stored RPE (#236).

    The projection selects ONLY ``plan_session_key`` and ``rpe`` — the
    ``notes_encrypted`` ciphertext column is never selected and never
    decrypted on this path (C-S2-2), so no note text can reach the digest.
    """
    result = await session.execute(
        select(DebriefRow.plan_session_key, DebriefRow.rpe).where(
            DebriefRow.athlete_id == athlete_id
        )
    )
    return {key: rpe for key, rpe in result.all()}


def build_divergence_digest(
    *,
    planned: Sequence[PendingDebrief],
    debrief_rpes: Mapping[str, int],
    today: date,
) -> DivergenceDigest | None:
    """Derive the approved week's plan-vs-reality divergence digest (#236 AC1).

    ``planned`` carries the approved week's non-rest day slots (date + title
    — the :func:`parse_plan_day_rows` output for the approved draft's
    content); ``debrief_rpes`` maps this athlete's debrief session keys to
    their stored RPE. The diff is DATE-KEYED (C-S2-8, the
    ``adjusted_future_days``-style row-diff primitive — never title
    identity): a planned slot strictly before ``today`` without a debrief
    row is missed; a debriefed slot whose RPE maps to the ``high`` severity
    band is flagged. Debrief keys outside the planned slots never count
    (intersection semantics — a debrief for a session the plan no longer
    contains is not this week's divergence). ``today`` is ALWAYS injected by
    the caller — the request scope owns the clock boundary (N3).

    Returns ``None`` when the week shows no divergence (graceful empty
    state: no digest section at all).
    """
    today_iso = today.isoformat()
    past = [slot for slot in planned if slot.plan_session_key < today_iso]
    missed_count = sum(1 for slot in past if slot.plan_session_key not in debrief_rpes)
    flagged_sessions = [
        FlaggedExertionFact(
            plan_session_key=slot.plan_session_key,
            session_title=_digest_single_line(slot.session_title),
            severity=rpe_severity_band(debrief_rpes[slot.plan_session_key]),
        )
        for slot in past
        if slot.plan_session_key in debrief_rpes
        and rpe_severity_band(debrief_rpes[slot.plan_session_key]) == "high"
    ]
    if not missed_count and not flagged_sessions:
        return None
    return DivergenceDigest(
        missed_count=missed_count,
        flagged_count=len(flagged_sessions),
        flagged_sessions=flagged_sessions,
    )


def divergence_digest_text(digest: DivergenceDigest) -> str:
    """Render the token-budgeted divergence digest text for the coach prompt.

    Fact strings are assembled SERVER-SIDE from counts + enum labels only
    (C-S2-3): the counts line mirrors AC1's shape ("N sessions missed · M
    sessions flagged <band> exertion", the band label joined from the
    facts' severity chips) and each flagged session renders one line with
    its own chip. The per-day lines are appended while the whole digest
    stays within :data:`DIVERGENCE_DIGEST_MAX_CHARS` (DoS bound).
    """
    counts: list[str] = []
    if digest.missed_count:
        counts.append(
            f"{digest.missed_count} session{'s' if digest.missed_count != 1 else ''} missed"
        )
    if digest.flagged_count:
        bands = " and ".join(sorted({fact.severity for fact in digest.flagged_sessions}))
        counts.append(
            f"{digest.flagged_count} session{'s' if digest.flagged_count != 1 else ''} "
            f"flagged {bands} exertion"
        )
    lines = ["- " + " · ".join(counts)]
    for fact in digest.flagged_sessions:
        candidate = (
            f"- {fact.plan_session_key}: {fact.session_title} — flagged {fact.severity} exertion"
        )
        candidate_digest = "\n".join([DIVERGENCE_DIGEST_HEADER, *lines, candidate])
        if len(candidate_digest) > DIVERGENCE_DIGEST_MAX_CHARS:
            break
        lines.append(candidate)
    return "\n".join([DIVERGENCE_DIGEST_HEADER, *lines])


def apply_divergence_digest(system_prompt: str, digest: DivergenceDigest | None) -> str:
    """Append the divergence digest to a base system prompt (#236 AC5).

    A no-op when the week shows no divergence — the prompt stays
    byte-identical, so the digest can never degrade an unrelated turn.
    """
    if digest is None:
        return system_prompt
    return f"{system_prompt}\n\n{divergence_digest_text(digest)}"
