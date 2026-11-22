"""Standing athlete preferences: environment enum & bounded free text (#419).

Extends the MADR-0026 ``agent_settings`` row (migration 0007) with the
MADR-0032 §3 / C8 preference set:

- ``environment_preference`` — nullable CHECK enum
  ``'indoor_first' | 'outdoor_first' | 'mixed'``. The health-adjacent
  session-environment intent lives in this structured enum only; the
  assembly seam maps it server-side to a fixed directive.
- ``standing_preferences`` — nullable C1 plaintext free text for
  non-medical logistical/session-format habits, capped at **1000**
  characters (CHECK). The text is transmitted to the model provider on
  every turn and disclosed at save time (MADR-0026 C4).

SQLite ``ALTER TABLE`` cannot add CHECK constraints in place, so both
columns and their CHECKs are applied through Alembic batch mode
(``render_as_batch`` — create-new-table / copy / drop / rename). The
downgrade reverses exactly those operations: the rebuilt table keeps the
0007 columns, data and constraints, while the two #419 columns and their
CHECKs are dropped. No backfill: both columns are nullable (unset).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two standing-preference columns (MADR-0032 §3, C8).

    The CHECK expressions and the 1000-character cap are hardcoded
    deliberately (migrations stay self-contained, never importing
    application symbols); the ORM mirror lives in
    ``coach_web.agent_settings.AgentSettingsRow.__table_args__`` — keep the
    two in sync when the schema changes.
    """
    with op.batch_alter_table("agent_settings", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("environment_preference", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("standing_preferences", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            "ck_agent_settings_environment_preference",
            "environment_preference IS NULL OR environment_preference IN "
            "('indoor_first', 'outdoor_first', 'mixed')",
        )
        batch_op.create_check_constraint(
            "ck_agent_settings_standing_preferences_len",
            "standing_preferences IS NULL OR length(standing_preferences) <= 1000",
        )


def downgrade() -> None:
    """Drop the two standing-preference columns (reversible, data-preserving)."""
    with op.batch_alter_table("agent_settings", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_agent_settings_standing_preferences_len", type_="check")
        batch_op.drop_constraint("ck_agent_settings_environment_preference", type_="check")
        batch_op.drop_column("standing_preferences")
        batch_op.drop_column("environment_preference")
