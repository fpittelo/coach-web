"""Persistence hygiene maintenance commands (#274, parent epic #161).

After migration 0006's in-place RPE encryption, plaintext residue persists in
SQLite free space (deleted pages remain until VACUUM). This module provides
the standalone maintenance commands closing that gap:

- ``vacuum``  — run ``VACUUM`` on the coach database so deleted plaintext
  pages are reclaimed (AC1). VACUUM sidesteps free-space scanning entirely:
  it rebuilds the database file from its live pages, so the residue is gone
  rather than merely hidden.
- ``backup``  — produce an encrypted-at-rest backup artifact of the coach
  database (AC2). The artifact is a Fernet-encrypted envelope around the
  SQLite file bytes; the key comes from ``DATA_ENCRYPTION_KEY`` (the same
  key the app and migration 0006 use — no second key, per KIS), never
  hardcoded, never committed.
- ``restore`` — decrypt a backup artifact into a fresh database file
  (tested round-trip path for AC2).

Design (KIS/YAGNI): Fernet over SQLCipher — ``cryptography`` is already a
direct dependency (MADR-0008 C6, #189) and SQLCipher would swap the whole
database engine. The backup artifact is C2-derived and stays encrypted at
rest (MADR-0008); the plaintext database bytes exist only inside this
process's memory while the envelope is built or opened.

Run via ``python -m coach_web.maintenance <vacuum|backup|restore> …`` —
see ``docs/admin_guide.md`` for the runbook.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from coach_web.config import get_settings

_BACKUP_SUFFIX = ".backup"
"""Suffix appended to the default backup artifact path."""


def _backup_key() -> bytes:
    """Resolve the backup key from ``DATA_ENCRYPTION_KEY`` (settings, #274 AC2).

    The same Fernet key that encrypts the C2 fields at rest also encrypts the
    backup artifact — no second key to manage (KIS). Missing or invalid key
    fails closed with a message naming the env var, never its value.
    """
    key = get_settings().DATA_ENCRYPTION_KEY.get_secret_value()
    if not key:
        raise ValueError("DATA_ENCRYPTION_KEY is not configured — cannot derive the backup key")
    return key.encode("ascii")


def vacuum_database(db_path: str) -> None:
    """Run ``VACUUM`` on the SQLite database at *db_path* (#274 AC1).

    VACUUM rebuilds the database file from its live pages, reclaiming the
    free pages that deleted plaintext rows (e.g. migration 0006's in-place
    rewrite residue) leave behind. Requires a brief exclusive lock — run it
    while the lane is stopped or idle.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()


def backup_database(db_path: str, output_path: str, key: bytes) -> None:
    """Write a Fernet-encrypted backup of the database at *db_path* (#274 AC2).

    The SQLite file bytes (WAL already checkpointed by the read) are sealed
    into a single Fernet token written to *output_path*. The plaintext bytes
    exist only in this process's memory; the artifact is encrypted at rest
    (MADR-0008 — C2-derived material never lands on disk in the clear).
    """
    import sqlite3

    if not key:
        raise ValueError("backup key is empty — refusing to write a plaintext backup")
    # Ensure any WAL content is folded into the main file before reading it.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    plaintext = Path(db_path).read_bytes()
    token = Fernet(key).encrypt(plaintext)
    Path(output_path).write_bytes(token)


def restore_database(backup_path: str, output_path: str, key: bytes) -> None:
    """Restore a Fernet-encrypted backup artifact into a fresh database file.

    The token is verified+decrypted first (wrong key → ``InvalidToken``),
    then the plaintext bytes are written via a temp file that is renamed
    into place only on success — a failed restore never leaves a
    half-written database behind (fail-closed, mirroring migration 0006).
    """
    token = Path(backup_path).read_bytes()
    plaintext = Fernet(key).decrypt(token)  # raises InvalidToken on a wrong key
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(Path(output_path).parent), suffix=_BACKUP_SUFFIX)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(plaintext)
        os.replace(tmp_name, output_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m coach_web.maintenance <verb> …``."""
    parser = argparse.ArgumentParser(
        prog="python -m coach_web.maintenance",
        description="Coach Web persistence hygiene (#274): vacuum, encrypted backup, restore.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("vacuum", help="Reclaim free pages (deleted plaintext residue) via VACUUM.")

    backup = sub.add_parser("backup", help="Write a Fernet-encrypted backup artifact.")
    backup.add_argument(
        "--output",
        default="",
        help="Backup artifact path (default: <COACH_DB_PATH>.backup).",
    )

    restore = sub.add_parser("restore", help="Restore a backup artifact into a fresh database.")
    restore.add_argument("backup", help="Path to the encrypted backup artifact.")
    restore.add_argument(
        "--output",
        default="",
        help="Restored database path (default: <COACH_DB_PATH>.restored).",
    )

    args = parser.parse_args(argv)
    db_path = get_settings().COACH_DB_PATH
    try:
        if args.command == "vacuum":
            vacuum_database(db_path)
        elif args.command == "backup":
            output = args.output or f"{db_path}{_BACKUP_SUFFIX}"
            backup_database(db_path, output, _backup_key())
            print(f"Encrypted backup written to {output}")  # noqa: T201
        elif args.command == "restore":
            output = args.output or f"{db_path}.restored"
            restore_database(args.backup, output, _backup_key())
            print(f"Database restored to {output}")  # noqa: T201
    except (ValueError, InvalidToken, OSError) as exc:
        print(f"maintenance failed: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
