"""Agent settings: admin orientation & response preferences (MADR-0026, #361).

Creates the ``agent_settings`` table — one row per athlete carrying the
owner-authored coach orientation (MADR-0026 C1 **plaintext**: the text is
transmitted to the model provider on every turn, so encrypting at rest would
not prevent that disclosure — it is disclosed at save time instead) plus the
structured response preferences (``verbosity``/``tone`` enums — the
health-adjacent semantics live in fixed enum columns, never in free text,
nLPD posture). ``orientation`` is capped at 1500 characters post-normalisation
(CHECK); ``athlete_id`` is UNIQUE — a single settings row per athlete,
upserted last-writer-wins and reset (deleted) by the admin surface (#362).

Mirrors the ``athlete_objectives`` conventions (incremental migration,
MADR-008 migration strategy). No backfill: no pre-existing settings exist
(the table is new). Down-migration drops the table — data-destructive by
design (documented per MADR-008; the reset path is one API call away).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create agent_settings (C1 plaintext per MADR-0026).

    The CHECK enums and the character cap are hardcoded deliberately
    (migrations stay self-contained, never importing application symbols);
    the ORM mirror lives in ``coach_web.agent_settings.AgentSettingsRow.
    __table_args__`` — keep the two in sync when the schema changes.
    """
    op.create_table(
        "agent_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("orientation", sa.Text(), nullable=True),
        sa.Column("verbosity", sa.Text(), nullable=True),
        sa.Column("tone", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(orientation) <= 1500",
            name="ck_agent_settings_orientation_length",
        ),
        sa.CheckConstraint(
            "verbosity IN ('concise', 'standard', 'detailed')",
            name="ck_agent_settings_verbosity",
        ),
        sa.CheckConstraint(
            "tone IN ('strict', 'balanced', 'supportive')",
            name="ck_agent_settings_tone",
        ),
        sa.UniqueConstraint("athlete_id", name="uq_agent_settings_athlete_id"),
    )


def downgrade() -> None:
    """Drop agent_settings (data-destructive; documented per MADR-0026)."""
    op.drop_table("agent_settings")
