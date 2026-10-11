"""Debriefs: post-session RPE & encrypted notes (MADR-008 C2, #189).

Creates the ``debriefs`` table — the MADR-008 C2 data class (SENSITIVE
health data, nLPD Art. 5(c)). Each row is one post-session debrief: the
athlete dimension (per-athlete isolation, #189 AC4), the optional
Intervals.icu workout link (``session_external_id`` — populated by Story
3.2, nullable until then), the local planned-session key, the CHECK-bounded
RPE (1–10) and the free-text notes as Fernet ciphertext
(``notes_encrypted`` — NEVER plaintext at rest; the encryption key travels
in ``DATA_ENCRYPTION_KEY``, outside this schema). ``athlete_id`` and
``plan_session_key`` are indexed per the pending-debrief derivation
(completed plan sessions without a debrief row, MADR-0010). Incremental
migration per the amended MADR-008 migration strategy.

No backfill: no pre-existing debriefs exist (the table is new).

Down-migration drops the table — data-destructive by design (documented per
MADR-008; the table holds no data before the first debrief is saved).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create debriefs (C2 — notes Fernet-encrypted per MADR-008).

    The RPE range CHECK is hardcoded deliberately (migrations stay
    self-contained, never importing application symbols); the ORM mirror
    lives in ``coach_web.debrief.DebriefRow.__table_args__`` — keep the two
    in sync when the schema changes.
    """
    op.create_table(
        "debriefs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("session_external_id", sa.Integer(), nullable=True),
        sa.Column("plan_session_key", sa.Text(), nullable=False),
        sa.Column("rpe", sa.Integer(), nullable=False),
        sa.Column("notes_encrypted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("rpe BETWEEN 1 AND 10", name="ck_debriefs_rpe_range"),
    )
    op.create_index("ix_debriefs_athlete_id", "debriefs", ["athlete_id"])
    op.create_index("ix_debriefs_plan_session_key", "debriefs", ["plan_session_key"])


def downgrade() -> None:
    """Drop debriefs (data-destructive; documented per MADR-008)."""
    op.drop_index("ix_debriefs_plan_session_key", table_name="debriefs")
    op.drop_index("ix_debriefs_athlete_id", table_name="debriefs")
    op.drop_table("debriefs")