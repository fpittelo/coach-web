"""Initial persistence schema: athlete_objectives (MADR-008, issue #166).

Creates ONLY the ``athlete_objectives`` table — the first internal
persistence in Coach Web. The remaining MADR-008 tables (periodization_phases,
debrief_entries, plan_drafts, weekly_reviews, debrief_dismissal_flags) arrive
with their own stories as incremental migrations (KIS scoping of #166).

No backfill: there is no pre-existing data to migrate.

Down-migration drops the table — data-destructive by design (documented per
MADR-008; the table holds no data before the first profile is saved).

Revision ID: 0001
Revises:
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create athlete_objectives (C1 — plaintext per MADR-008)."""
    op.create_table(
        "athlete_objectives",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("objective_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("target_metric", sa.Text(), nullable=True),
        sa.Column("target_value", sa.Float(), nullable=True),
        sa.Column("target_date", sa.Text(), nullable=True),
        sa.Column("availability_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "objective_type IN ('outcome', 'process', 'milestone')",
            name="ck_athlete_objectives_objective_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'achieved', 'abandoned')",
            name="ck_athlete_objectives_status",
        ),
    )


def downgrade() -> None:
    """Drop athlete_objectives (data-destructive; documented per MADR-008)."""
    op.drop_table("athlete_objectives")
