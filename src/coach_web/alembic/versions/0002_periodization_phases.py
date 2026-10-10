"""Periodization phases: macrocycle phase plan per objective (MADR-008, #167).

Creates the ``periodization_phases`` table — the second MADR-008 C1 data
class (plaintext, tactical coaching state). Each row is one macrocycle phase
hanging off its parent ``athlete_objectives`` row with ``ON DELETE CASCADE``
(a phase is meaningless without its objective); ``objective_id`` is indexed
per the ADR index list. Incremental migration per the amended MADR-008
migration strategy: the remaining tables arrive with their own stories.

No backfill: no pre-existing phases exist (the table is new).

Down-migration drops the table — data-destructive by design (documented per
MADR-008; the table holds no data before the first plan is saved).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create periodization_phases (C1 — plaintext per MADR-008)."""
    op.create_table(
        "periodization_phases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "objective_id",
            sa.Integer(),
            sa.ForeignKey("athlete_objectives.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phase_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Text(), nullable=False),
        sa.Column("end_date", sa.Text(), nullable=False),
        sa.Column("focus", sa.Text(), nullable=True),
        sa.Column("weekly_hours_target", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "phase_type IN ('base', 'build', 'peak', 'taper', 'recovery', 'competition')",
            name="ck_periodization_phases_phase_type",
        ),
        sa.CheckConstraint(
            "start_date <= end_date",
            name="ck_periodization_phases_date_order",
        ),
    )
    op.create_index(
        "ix_periodization_phases_objective_id",
        "periodization_phases",
        ["objective_id"],
    )


def downgrade() -> None:
    """Drop periodization_phases (data-destructive; documented per MADR-008)."""
    op.drop_index("ix_periodization_phases_objective_id", table_name="periodization_phases")
    op.drop_table("periodization_phases")
