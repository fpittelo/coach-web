"""Conversation memory: server-owned threads & messages (MADR-0025, #348).

Creates the two conversation-memory tables (MADR-0025 schema, migration
**0008** — head 0007 was taken by ``agent_settings``, #361):

- ``conversation_threads`` — one row per server-owned thread. ``status`` is
  CHECK-bounded to the fixed ``active|archived`` enum (C4: archived threads
  are read-only). The rolling summary rides along as Fernet ciphertext
  (``summary_encrypted`` — C1, the ``*_encrypted`` convention shared with
  the debriefs; NO plaintext summary column) plus its ``summary_updated_at``
  timestamp, both nullable until a summary is generated (#350's fixed-prompt
  summarizer; this slice only exposes the storage shape). Indexed on
  ``(athlete_id, status, last_active_at)`` for the active-thread lookup and
  the retention sweep.
- ``conversation_messages`` — the raw turns. ``thread_id`` FK-cascades onto
  its parent thread (deleting a thread deletes its messages); ``role`` is
  CHECK-bounded to the server-assigned ``user|assistant`` enum (C4: never
  client-supplied); content is Fernet ciphertext (``content_encrypted`` —
  C1; the column is NOT NULL: a message row cannot exist without its
  ciphertext, there is NO plaintext content column and no plaintext
  fallback). ``seq`` is the per-thread monotonic order key, UNIQUE per
  thread. Indexed on ``(thread_id, seq)``. Metadata (ids, role, seq,
  timestamps) stays plaintext (accepted residual R3, MADR-0008 F9 pattern).

Mirrors the ``agent_settings`` conventions (incremental migration, MADR-008
migration strategy). No backfill: no pre-existing conversations exist (the
tables are new). Down-migration drops both tables — data-destructive by
design (documented per MADR-008; the memory is a convenience layer).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the conversation memory tables (MADR-0025, C1/C4).

    The CHECK enums and the CASCADE are hardcoded deliberately (migrations
    stay self-contained, never importing application symbols); the ORM
    mirror lives in ``coach_web.conversation.ConversationThreadRow`` and
    ``ConversationMessageRow.__table_args__`` — keep the two in sync when
    the schema changes.
    """
    op.create_table(
        "conversation_threads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("last_active_at", sa.Text(), nullable=False),
        sa.Column("summary_encrypted", sa.Text(), nullable=True),
        sa.Column("summary_updated_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_conversation_threads_status",
        ),
    )
    op.create_index(
        "ix_conversation_threads_athlete_status_last_active",
        "conversation_threads",
        ["athlete_id", "status", "last_active_at"],
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "thread_id",
            sa.Integer(),
            sa.ForeignKey("conversation_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content_encrypted", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
    )
    # One object serving both mandated shapes: the (thread_id, seq) index
    # IS the per-thread seq UNIQUE (KIS).
    op.create_index(
        "uq_conversation_messages_thread_seq",
        "conversation_messages",
        ["thread_id", "seq"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the conversation memory tables (data-destructive; documented)."""
    op.drop_index("uq_conversation_messages_thread_seq", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index(
        "ix_conversation_threads_athlete_status_last_active", table_name="conversation_threads"
    )
    op.drop_table("conversation_threads")
