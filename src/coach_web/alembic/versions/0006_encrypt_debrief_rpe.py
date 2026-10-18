"""Debrief RPE: encrypt at rest (MADR-0008 C2 classification drift, #243).

The #236 S2 routing review (residual risk R3) found the ``debriefs.rpe``
column drifting from its MADR-0008 C2 classification: the free-text notes
were Fernet-encrypted (``notes_encrypted``) while the RPE stayed a plaintext
Integer — host-local file access to the SQLite file could read exertion data
in clear text (Swiss nLPD scope). This migration closes the drift IN PLACE:

- upgrade: the column adopts the ``notes_encrypted`` typing/nullable pattern
  (Text, nullable) and the numeric range CHECK is dropped (a ciphertext
  column cannot carry ``rpe BETWEEN 1 AND 10`` — the 1–10 bound lives at the
  Pydantic boundary), then every existing plaintext RPE is encrypted in
  place with the lane's ``DATA_ENCRYPTION_KEY`` (the same key the app's
  ``build_debrief_cipher`` consumes — no second key). Rows are read and
  rewritten row-by-row; a NULL RPE stays NULL.
- downgrade: every stored token is decrypted back to the plaintext integer
  and the column restores the Integer NOT NULL + CHECK shape (dev rollback
  symmetry). A tampered NULL RPE fails the NOT NULL restore — fail-closed,
  never silently dropped.

The key is read from the environment directly (migrations stay
self-contained, never importing application symbols). A missing or invalid
key FAILS the migration — per MADR-008 a failed migration aborts startup,
so a lane can never come up with plaintext RPEs left behind or undecryptable
tokens ahead.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RPE_RANGE_CHECK = "rpe BETWEEN 1 AND 10"
"""The restored CHECK DDL — mirrors migration 0004's constraint verbatim."""


def _migration_cipher() -> Fernet:
    """Build the migration's Fernet from ``DATA_ENCRYPTION_KEY``.

    Missing or invalid key → the migration fails (MADR-008: a failed
    migration aborts startup). The message names the env var, never its
    value.
    """
    key = os.environ.get("DATA_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("DATA_ENCRYPTION_KEY is not configured — cannot migrate debrief RPEs")
    return Fernet(key)


def upgrade() -> None:
    """Encrypt every stored RPE in place (MADR-0008 C2, #243)."""
    # Key first (#306): resolve/validate the cipher BEFORE any DDL executes.
    # A missing/invalid key must fail the migration cleanly — the batch
    # alter below must never run (and never leave a _alembic_tmp_* residue)
    # when the key is absent.
    cipher = _migration_cipher()
    conn = op.get_bind()
    # Recovery (#306): the batch alter's transient _alembic_tmp_debriefs
    # table persisted after previously interrupted (non-atomic) runs and
    # bricked every subsequent start with "table already exists". The table
    # is transient by design — dropping a leftover before the batch alter
    # makes recovery from interrupted runs automatic.
    conn.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_debriefs"))
    # Schema first: the numeric range CHECK must go before a token can be
    # written, and the column adopts the notes_encrypted typing/nullable
    # pattern (batch-rendered per env.py's SQLite ALTER strategy).
    with op.batch_alter_table("debriefs") as batch_op:
        batch_op.alter_column(
            "rpe",
            existing_type=sa.Integer(),
            type_=sa.Text(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.drop_constraint("ck_debriefs_rpe_range", type_="check")
    # Data: encrypt each plaintext RPE in place (NULL stays NULL). The
    # plaintext value exists only inside this migration process — never in a
    # log, never in the token.
    rows = conn.execute(sa.text("SELECT id, rpe FROM debriefs WHERE rpe IS NOT NULL")).all()
    for row_id, plaintext in rows:
        token = cipher.encrypt(str(int(plaintext)).encode("utf-8")).decode("ascii")
        conn.execute(
            sa.text("UPDATE debriefs SET rpe = :token WHERE id = :id"),
            {"token": token, "id": row_id},
        )


def downgrade() -> None:
    """Decrypt the stored RPE tokens back to plaintext integers (#243)."""
    cipher = _migration_cipher()
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, rpe FROM debriefs WHERE rpe IS NOT NULL")).all()
    for row_id, token in rows:
        plaintext = int(cipher.decrypt(str(token).encode("ascii")).decode("utf-8"))
        conn.execute(
            sa.text("UPDATE debriefs SET rpe = :value WHERE id = :id"),
            {"value": plaintext, "id": row_id},
        )
    # Schema last: restore the pre-#243 Integer NOT NULL + CHECK shape. A
    # tampered NULL RPE fails the NOT NULL restore (fail-closed, documented).
    with op.batch_alter_table("debriefs") as batch_op:
        batch_op.alter_column(
            "rpe",
            existing_type=sa.Text(),
            type_=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.create_check_constraint("ck_debriefs_rpe_range", _RPE_RANGE_CHECK)
