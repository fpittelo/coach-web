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
"""

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import CheckConstraint, Integer, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from coach_web.auth.whitelist import normalize_email
from coach_web.config import Settings
from coach_web.db import Base
from coach_web.errors import (
    EncryptionReason,
    EncryptionUnavailableError,
    MirrorReason,
)
from coach_web.microcycle import MAX_SESSION_TITLE_LENGTH, PlanDraftRow
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
    session (the ICU workout id arrives with Story 3.2 as
    ``session_external_id``).
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
