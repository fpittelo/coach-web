"""Plan drafts: weekly microcycle drafts & approval states (MADR-008, #168).

Creates the ``plan_drafts`` table — the MADR-008 C3 data class (plaintext,
operational workflow state; plan text already exists in the GitHub plan
branch at the same sensitivity). Each row is one proposed weekly microcycle
carrying its ISO week bounds (CHECK-ordered), the Markdown rendition, the
draft state machine status (draft → submitted → approved/rejected; superseded
on replacement), the approval timestamp and the optional plan-issue link.
``week_start_date`` is indexed per the ADR index list. Incremental migration
per the amended MADR-008 migration strategy.

No backfill: no pre-existing drafts exist (the table is new).

Down-migration drops the table — data-destructive by design (documented per
MADR-008; the table holds no data before the first draft is saved).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create plan_drafts (C3 — plaintext per MADR-008)."""
    op.create_table(
        "plan_drafts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("week_start_date", sa.Text(), nullable=False),
        sa.Column("week_end_date", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("approved_at", sa.Text(), nullable=True),
        sa.Column("github_issue_ref", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "week_start_date <= week_end_date",
            name="ck_plan_drafts_week_range",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'superseded')",
            name="ck_plan_drafts_status",
        ),
    )
    op.create_index(
        "ix_plan_drafts_week_start_date",
        "plan_drafts",
        ["week_start_date"],
    )


def downgrade() -> None:
    """Drop plan_drafts (data-destructive; documented per MADR-008)."""
    op.drop_index("ix_plan_drafts_week_start_date", table_name="plan_drafts")
    op.drop_table("plan_drafts")
