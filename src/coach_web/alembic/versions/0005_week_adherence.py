"""Week adherence: weekly plan-adherence metrics (MADR-008 C3, #234).

Creates the ``week_adherence`` table — the MADR-008 C3 data class
(plaintext operational metrics). Each row is one ISO week's plan-adherence
metric for one athlete: the week key (``YYYY-Www``), the planned-session
count (from approved plan drafts' day rows), the completed-session count
(from the athlete's debrief rows), the adherence percentage and the
computation timestamp. COUNTS AND PERCENTAGES ONLY — no RPE, no notes, no
wellness values (the C3 classification stays honest; the health content
remains in the C2 ``debriefs`` table). The (athlete_id, week_key) pair is
UNIQUE — the debrief-save upsert (#234) is idempotent per athlete and week
— and the percentage is CHECK-bounded to 0..100. Incremental migration per
the amended MADR-008 migration strategy.

No backfill: no pre-existing metric rows exist (the table is new; the first
debrief save after this migration writes the first row).

Down-migration drops the table — data-destructive by design (documented per
MADR-008; the table holds derived counts only, recomputed on the next
debrief save).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create week_adherence (C3 — counts/percentages only per MADR-008).

    The percentage range CHECK is hardcoded deliberately (migrations stay
    self-contained, never importing application symbols); the ORM mirror
    lives in ``coach_web.debrief.WeekAdherenceRow.__table_args__`` — keep
    the two in sync when the schema changes.
    """
    op.create_table(
        "week_adherence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("week_key", sa.Text(), nullable=False),
        sa.Column("planned_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("adherence_pct", sa.Float(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
        sa.CheckConstraint("adherence_pct BETWEEN 0 AND 100", name="ck_week_adherence_pct_range"),
        sa.UniqueConstraint("athlete_id", "week_key", name="uq_week_adherence_athlete_week"),
    )


def downgrade() -> None:
    """Drop week_adherence (data-destructive; documented per MADR-008)."""
    op.drop_table("week_adherence")
