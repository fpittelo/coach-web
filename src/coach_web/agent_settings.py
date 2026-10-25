"""Agent settings store — owner orientation & response preferences (#361).

Persistence for MADR-0026: one row per athlete holding the owner-authored
coach ``orientation`` (C1 plaintext, capped at 1500 characters) and the
structured response preferences (``verbosity``/``tone`` CHECK enums —
health-adjacent semantics live in fixed enum columns, never in free text,
nLPD posture). The row is read per request by the assembly seam
(:func:`coach_web.agent.assemble_system_prompt`) and mutated by the admin
API (#362); reset-to-default is a row delete.

Per-athlete isolation: every query is scoped by ``athlete_id`` like every
Coach Web store. The save is last-writer-wins (single owner, n=1). The row
is treated as UNTRUSTED input at the seam — enum/nullability structure is
validated here into the seam-boundary model, and the free text is sanitized
at assembly (sanitizer call site 2, MADR-0026 cyber C2); a row corrupted
out-of-band raises :class:`pydantic.ValidationError`, which the request
scope routes into the digest-degrade family (fail open, cyber C6).
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import CheckConstraint, Integer, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from coach_web.db import Base

AGENT_ORIENTATION_MAX_CHARS = 1500
"""MADR-0026 cap on the persisted orientation (characters, post-normalisation).

Hardcoded as a CHECK in migration 0007 — keep the two in sync. The generalised
sanitizer (:func:`coach_web.agent.sanitize_untrusted_text`) enforces the same
cap at the orientation call sites (save + assembly).
"""

Verbosity = Literal["concise", "standard", "detailed"]
"""MADR-0026 ``verbosity`` CHECK enum."""

Tone = Literal["strict", "balanced", "supportive"]
"""MADR-0026 ``tone`` CHECK enum."""


class AgentSettings(BaseModel):
    """Seam-boundary view of an ``agent_settings`` row (MADR-0026, #361).

    This model validates the row's STRUCTURE at read time (enums, nullability)
    — it deliberately carries NO length validator on ``orientation``: a
    schema-tampered oversize row must reach the assembly seam and be CLAMPED
    by the read-time sanitizer, not fail the read (MADR-0026 failure posture).
    Save-time length enforcement belongs to the API layer (#362: sanitize →
    validate → 422) plus the DB CHECK backstop.
    """

    orientation: str | None = Field(
        default=None,
        description=(
            "Owner-authored coach orientation (C1 plaintext, untrusted until "
            "sanitized at assembly — never length-checked at this seam)"
        ),
    )
    verbosity: Verbosity | None = Field(
        default=None,
        description="Response verbosity preference (MADR-0026 enum)",
    )
    tone: Tone | None = Field(
        default=None,
        description="Coaching tone preference (MADR-0026 enum)",
    )


class AgentSettingsSaveRequest(BaseModel):
    """PUT ``/api/admin/agent-settings`` request body (#362, MADR-0026).

    A FULL representation: all three fields are required (JSON ``null``
    allowed for "unset") — an omitted field is a malformed payload (422),
    never an implicit clear. The free text is UNTRUSTED input whose boundary
    type is deliberately loose (``str | None``): the save-site sanitizer +
    cap validation live in the API layer (#362), which stores only the
    sanitized text; the enum fields are CHECK-enum literals.
    """

    orientation: str | None
    verbosity: Verbosity | None
    tone: Tone | None


class AgentSettingsRow(Base):
    """ORM model for the MADR-0026 ``agent_settings`` table (C1, plaintext)."""

    __tablename__ = "agent_settings"
    __table_args__ = (
        CheckConstraint(
            "length(orientation) <= 1500",
            name="ck_agent_settings_orientation_length",
        ),
        CheckConstraint(
            "verbosity IN ('concise', 'standard', 'detailed')",
            name="ck_agent_settings_verbosity",
        ),
        CheckConstraint(
            "tone IN ('strict', 'balanced', 'supportive')",
            name="ck_agent_settings_tone",
        ),
        UniqueConstraint("athlete_id", name="uq_agent_settings_athlete_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False)
    orientation: Mapped[str | None] = mapped_column(Text, nullable=True)
    verbosity: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (MADR-008 convention)."""
    return datetime.now(UTC).isoformat()


async def get_agent_settings(session: AsyncSession, *, athlete_id: int) -> AgentSettings | None:
    """Return the athlete's settings as the seam-boundary model, or None.

    ``None`` means "no row" — the assembly seam then emits the defaults
    (core-only prompt, byte-equivalent to the pre-settings behaviour, AC4).
    A row corrupted out-of-band (raw SQL bypassing the ORM) raises
    :class:`pydantic.ValidationError` from the enum validation — the caller
    routes it into the digest-degrade family (fail open, cyber C6). The
    orientation text is NOT validated for length or content here: it is
    untrusted input, sanitized by the seam at assembly.
    """
    result = await session.execute(
        select(AgentSettingsRow).where(AgentSettingsRow.athlete_id == athlete_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return AgentSettings.model_validate(
        {
            "orientation": row.orientation,
            "verbosity": row.verbosity,
            "tone": row.tone,
        }
    )


async def save_agent_settings(
    session: AsyncSession,
    *,
    athlete_id: int,
    orientation: str | None,
    verbosity: str | None,
    tone: str | None,
) -> AgentSettingsRow:
    """Upsert the athlete's single settings row (last-writer-wins) and commit.

    The caller owns the save-time sanitization/validation contract (API #362:
    sanitize → validate → 422); the DB CHECK constraints backstop the enum
    values and the 1500-character cap for every writer (AC1).
    """
    result = await session.execute(
        select(AgentSettingsRow).where(AgentSettingsRow.athlete_id == athlete_id)
    )
    row = result.scalar_one_or_none()
    now = _utc_now_iso()
    if row is None:
        row = AgentSettingsRow(athlete_id=athlete_id, updated_at=now)
        session.add(row)
    row.orientation = orientation
    row.verbosity = verbosity
    row.tone = tone
    row.updated_at = now
    await session.commit()
    return row


async def delete_agent_settings(session: AsyncSession, *, athlete_id: int) -> bool:
    """Delete the athlete's settings row (reset to defaults) and commit.

    Returns ``True`` when a row was removed, ``False`` when the athlete had
    none (the reset is idempotent — the admin surface's reset path).
    """
    result = await session.execute(
        select(AgentSettingsRow).where(AgentSettingsRow.athlete_id == athlete_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True
