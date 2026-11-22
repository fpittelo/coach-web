"""Durable encrypted GCS snapshot store (MADR-0032, issue #417).

The Cloud Run lane keeps SQLite on per-instance tmpfs (``/data``) — MADR-0032
leaves ``db.py`` and its WAL semantics untouched — and gains durability with a
single whole-file encrypted snapshot in a dedicated regional GCS bucket:

- **Boot restore** (``python -m coach_web.snapshot boot-restore``) runs BEFORE
  ``alembic upgrade head`` in the container entrypoint. It implements the
  fail-closed anti-clobber state machine (C2/C6) in the fixed order
  ``download → Fernet authenticate/decrypt → PRAGMA integrity_check → alembic
  upgrade head (staged file) → atomic activation``: only a confirmed ``404``
  initializes an empty database (migrated to ``head`` before publication is
  enabled); when the object exists, the bytes are authenticated/decrypted with
  the lane Fernet key BEFORE SQLite parses them, integrity-checked, **migrated
  while still staged**, then atomically activated. Any other outcome leaves
  publication hard-disabled and exits non-zero, so an empty database can never
  boot and overwrite the durable copy.
- **Publication** (``SnapshotCoordinator``) builds a WAL-safe snapshot
  (``VACUUM INTO``), seals the whole file with ``Fernet(DATA_ENCRYPTION_KEY)``
  and uploads it with an ``ifGenerationMatch`` precondition. A ``412`` means
  this instance is stale: it hard-disables further uploads instead of
  overwriting a newer writer.

Transport is the GCS JSON API v1 over HTTPS using the Cloud Run Metadata
Server token endpoint — no ``google-cloud-storage`` dependency (KIS/YAGNI:
``httpx`` and ``cryptography`` are already direct dependencies).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from coach_web.config import Settings, get_settings
from coach_web.conversation_api import log_persistence_degrade
from coach_web.debrief import resolve_data_encryption_key

logger = logging.getLogger("coach_web")

GCS_BASE_URL = "https://storage.googleapis.com"
"""GCS JSON API v1 origin — HTTPS only (MADR-0032 C1)."""

METADATA_TOKEN_URL = (
    # A public metadata endpoint, not a credential — bandit's B105 heuristic
    # matches the "TOKEN" in the constant name.
    "http://metadata.google.internal/computeMetadata/v1/instance"  # noqa: S105
    "/service-accounts/default/token"
)
"""Cloud Run Metadata Server token endpoint (Google's documented metadata host)."""

STATE_SUFFIX = ".gcs_state"
"""Suffix of the local boot-state file: ``<COACH_DB_PATH>.gcs_state``."""

_HTTP_TIMEOUT_SECONDS = 30.0
_TOKEN_TIMEOUT_SECONDS = 10.0
_TOKEN_EXPIRY_SKEW_SECONDS = 60.0


class SnapshotError(Exception):
    """Base snapshot failure — messages never carry database or key content."""


class SnapshotUnavailable(SnapshotError):
    """A transient or unexpected GCS outcome (5xx, timeout, 401/403, malformed JSON)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SnapshotConflict(SnapshotError):
    """``412 Precondition Failed`` — another writer advanced the generation."""


class SnapshotStateError(SnapshotError):
    """The local boot state is missing/invalid or publication is hard-disabled."""


@dataclass(frozen=True)
class BootState:
    """The generation/publication tuple persisted next to the database."""

    generation: int
    publish_enabled: bool


class TokenProvider(Protocol):
    """Minimal async access-token provider (injectable for tests)."""

    async def token(self) -> str:
        """Return a bearer token for the GCS JSON API."""


class SnapshotStore(Protocol):
    """The generation-guarded object-store contract used by boot/publish."""

    async def object_generation(self) -> int | None:
        """Return the live object generation, or ``None`` on a confirmed 404."""

    async def download(self, generation: int) -> bytes:
        """Return the media bytes pinned to *generation*."""

    async def upload(self, ciphertext: bytes, expected_generation: int) -> int:
        """Upload *ciphertext* iff the live generation matches; return the new one."""


# ---------------------------------------------------------------------------
# Boot state file
# ---------------------------------------------------------------------------


def snapshot_state_path(db_path: str | Path) -> Path:
    """The boot-state file path for *db_path* (sibling, owner-only)."""
    return Path(f"{db_path}{STATE_SUFFIX}")


def read_boot_state(db_path: str | Path) -> BootState | None:
    """Read the boot state; malformed or invalid content reads as ``None``."""
    path = snapshot_state_path(db_path)
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    generation = raw.get("generation")
    publish_enabled = raw.get("publish_enabled")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return None
    if not isinstance(publish_enabled, bool):
        return None
    return BootState(generation=generation, publish_enabled=publish_enabled)


def write_boot_state(db_path: str | Path, state: BootState) -> Path:
    """Atomically write the boot state with owner-only ``0600`` permissions."""
    path = snapshot_state_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(
                {"generation": state.generation, "publish_enabled": state.publish_enabled},
                handle,
            )
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


# ---------------------------------------------------------------------------
# Metadata Server token
# ---------------------------------------------------------------------------


class MetadataTokenProvider:
    """Cache the Cloud Run service-account access token until near expiry."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._token: str | None = None
        self._expires_at = 0.0

    async def _request(self) -> httpx.Response:
        if self._client is not None:
            return await self._client.get(
                METADATA_TOKEN_URL,
                headers={"Metadata-Flavor": "Google"},
                timeout=_TOKEN_TIMEOUT_SECONDS,
            )
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT_SECONDS) as client:
            return await client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})

    async def token(self) -> str:
        """Return a cached bearer token, refreshing only when near expiry."""
        now = time.monotonic()
        if self._token is not None and now < self._expires_at - _TOKEN_EXPIRY_SKEW_SECONDS:
            return self._token
        response = await self._request()
        if response.status_code != 200:
            raise SnapshotUnavailable("metadata token request failed", response.status_code)
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise SnapshotUnavailable("metadata token response was not JSON") from exc
        access_token = payload.get("access_token") if isinstance(payload, dict) else None
        expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise SnapshotUnavailable("metadata token response carried no access_token")
        lifetime = (
            float(expires_in)
            if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
            else 0.0
        )
        if lifetime <= 0:
            raise SnapshotUnavailable("metadata token response carried no expiry")
        self._token = access_token
        self._expires_at = time.monotonic() + lifetime
        return access_token


# ---------------------------------------------------------------------------
# GCS JSON API v1 client
# ---------------------------------------------------------------------------


def _parse_generation(response: httpx.Response) -> int:
    """Parse ``generation`` from a GCS JSON body — malformed fails closed."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise SnapshotUnavailable("GCS response was not JSON", response.status_code) from exc
    generation = payload.get("generation") if isinstance(payload, dict) else None
    if isinstance(generation, bool):
        raise SnapshotUnavailable("GCS generation was not an integer", response.status_code)
    if isinstance(generation, int):
        parsed = generation
    elif isinstance(generation, str) and generation.isdigit():
        parsed = int(generation)
    else:
        raise SnapshotUnavailable("GCS generation was missing or malformed", response.status_code)
    if parsed < 1:
        raise SnapshotUnavailable("GCS generation was not positive", response.status_code)
    return parsed


class GcsSnapshotClient:
    """HTTPS-only GCS JSON API v1 client for the single canonical object."""

    def __init__(
        self,
        bucket: str,
        object_name: str,
        *,
        token_provider: TokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bucket = bucket
        self._object_name = object_name
        self._token_provider = token_provider or MetadataTokenProvider()
        self._client = client

    def _object_url(self) -> str:
        return f"{GCS_BASE_URL}/storage/v1/b/{quote(self._bucket, safe='')}/o/" + quote(
            self._object_name, safe=""
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {await self._token_provider.token()}"
        if self._client is not None:
            return await self._client.request(
                method, url, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS, **kwargs
            )
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            return await client.request(method, url, headers=headers, **kwargs)

    async def object_generation(self) -> int | None:
        """Probe the metadata endpoint: 404 is a confirmed absence, else parse."""
        response = await self._request("GET", self._object_url())
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SnapshotUnavailable("GCS metadata read failed", response.status_code)
        return _parse_generation(response)

    async def download(self, generation: int) -> bytes:
        """Download the media bytes pinned to *generation* (no noncurrent fallback)."""
        response = await self._request(
            "GET",
            self._object_url(),
            params={"alt": "media", "generation": str(generation)},
        )
        if response.status_code != 200:
            raise SnapshotUnavailable("GCS media download failed", response.status_code)
        return response.content

    async def upload(self, ciphertext: bytes, expected_generation: int) -> int:
        """Upload with ``ifGenerationMatch``; 412 aborts without overwriting."""
        response = await self._request(
            "POST",
            f"{GCS_BASE_URL}/upload/storage/v1/b/{quote(self._bucket, safe='')}/o",
            params={
                "uploadType": "media",
                "name": self._object_name,
                "ifGenerationMatch": str(expected_generation),
            },
            headers={"Content-Type": "application/octet-stream"},
            content=ciphertext,
        )
        if response.status_code == 412:
            raise SnapshotConflict("snapshot generation conflict")
        if response.status_code != 200:
            raise SnapshotUnavailable("GCS upload failed", response.status_code)
        return _parse_generation(response)


# ---------------------------------------------------------------------------
# WAL-safe builder, whole-file envelope and atomic restore
# ---------------------------------------------------------------------------


def build_snapshot_plaintext(db_path: str) -> bytes:
    """Build a consistent snapshot via ``VACUUM INTO`` (never a raw file copy).

    ``VACUUM INTO`` is SQLite's documented online-backup path: it reads a
    consistent image (committed WAL content included) and writes a compacted
    file, which is exactly what the retention purge needs — free pages that
    still hold deleted plaintext/ciphertext are reclaimed (MADR-0032 C2/C5).

    The plaintext never leaves an owner-only scope (C2): the target lives in a
    private ``0700`` directory and is an EXISTING empty ``0600`` file (SQLite
    accepts an empty VACUUM INTO target and keeps the inode it writes), so the
    database bytes are never group/other-readable for the duration of the
    vacuum. The whole scratch directory is removed in ``finally``.
    """
    db_file = Path(db_path)
    if not db_file.is_file():
        raise SnapshotError("database not found — refusing to snapshot an empty store")
    scratch_dir = tempfile.mkdtemp(dir=str(db_file.parent), prefix=".snapshot-")
    try:
        os.chmod(scratch_dir, 0o700)
        scratch = Path(scratch_dir) / "snapshot.db"
        fd = os.open(scratch, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("VACUUM INTO ?", (str(scratch),))
        finally:
            conn.close()
        return scratch.read_bytes()
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def encrypt_snapshot(db_path: str, key: bytes) -> bytes:
    """Seal the whole database file in the lane Fernet envelope (MADR-0032 C2).

    No raw SQLite bytes are ever uploaded: the returned token is the only
    artifact that leaves the process.
    """
    if not key:
        raise SnapshotError("snapshot key unavailable — refusing a plaintext snapshot")
    return Fernet(key).encrypt(build_snapshot_plaintext(db_path))


def _assert_sqlite_integrity(path: str) -> None:
    """Refuse a payload whose ``PRAGMA integrity_check`` is not exactly ``ok``."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    if rows != [("ok",)]:
        raise SnapshotError("snapshot integrity check failed")


def _migrate_sqlite_file(db_path: str) -> None:
    """Bring *db_path* to ``alembic upgrade head``, then restore the environment.

    ``coach_web/alembic/env.py`` derives its URL from ``COACH_DB_PATH``, so the
    variable is pointed at the target for the duration of the upgrade and
    restored in ``finally`` — the boot restore must migrate the STAGED file
    (MADR-0032 C2) without disturbing the lane's own setting.
    """
    previous = os.environ.get("COACH_DB_PATH")
    os.environ["COACH_DB_PATH"] = db_path
    try:
        command.upgrade(Config(str(Path(__file__).with_name("alembic.ini"))), "head")
    finally:
        if previous is None:
            os.environ.pop("COACH_DB_PATH", None)
        else:
            os.environ["COACH_DB_PATH"] = previous


def restore_snapshot(
    ciphertext: bytes,
    key: bytes,
    db_path: str,
    *,
    migrate_fn: Callable[[str], None] | None = None,
) -> None:
    """Authenticate, integrity-check, migrate and atomically activate a snapshot.

    The fixed C2 restore order is enforced here:

    1. ``Fernet.decrypt`` authenticates/decrypts FIRST — ``InvalidToken``
       aborts before SQLite ever parses the bytes.
    2. The plaintext is staged in an owner-only ``0600`` temp file.
    3. ``PRAGMA integrity_check`` validates the staged image.
    4. The STAGED file is migrated to ``head`` (``migrate_fn``, defaulting to
       :func:`_migrate_sqlite_file`) — a migration failure aborts with
       ``db_path`` untouched and no partial swap.
    5. Only then are stale ``-wal``/``-shm`` sidecars removed and the staged
       file renamed into place: atomic activation is LAST.
    """
    plaintext = Fernet(key).decrypt(ciphertext)  # InvalidToken => fail closed
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(db_file.parent), suffix=".snapshot-restore")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(plaintext)
        _assert_sqlite_integrity(tmp_name)
        (migrate_fn or _migrate_sqlite_file)(tmp_name)
        for suffix in ("-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        os.replace(tmp_name, db_file)
    finally:
        # No plaintext residue on any path, including SQLite sidecars a failed
        # migration may have left next to the staged file.
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{tmp_name}{suffix}").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Boot restore state machine (C2/C6)
# ---------------------------------------------------------------------------


def _mark_publish_disabled(db_path: str) -> None:
    """Best-effort ``publish_enabled=false`` marker for a failed boot."""
    try:
        write_boot_state(db_path, BootState(generation=0, publish_enabled=False))
    except OSError:
        logger.error("snapshot state write failed during fail-closed boot")


async def boot_restore(
    settings: Settings,
    *,
    store: SnapshotStore | None = None,
    key: bytes | None = None,
) -> int:
    """Run the fail-closed boot restore. Returns the process exit code.

    - Bucket unset → local lane, no-op exit ``0`` (AC4).
    - Confirmed ``404`` → the empty database is migrated to ``head`` and only
      then is publication enabled at generation ``0`` (first upload uses
      ``ifGenerationMatch=0``): ``publish_enabled=true`` is never asserted
      before the schema exists.
    - Object present → download/decrypt/integrity-check/migrate the staged file
      and activate it last; track the exact generation.
    - ANY error when the bucket is configured and the object is not a
      confirmed ``404`` → publication hard-disabled, redacted error log and
      exit ``1`` (startup aborts; the durable copy is never clobbered).
    """
    db_path = settings.COACH_DB_PATH
    bucket = settings.COACH_SNAPSHOT_BUCKET
    if not bucket:
        logger.info("event=snapshot_disabled reason=bucket_unset mode=local")
        return 0
    try:
        active_store = store or GcsSnapshotClient(bucket, settings.COACH_SNAPSHOT_OBJECT)
        generation = await active_store.object_generation()
        if generation is None:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _migrate_sqlite_file(db_path)
            write_boot_state(db_path, BootState(generation=0, publish_enabled=True))
            logger.info("event=snapshot_boot_absent bucket=%s generation=0", bucket)
            return 0
        resolved_key = key if key is not None else resolve_data_encryption_key(settings)
        ciphertext = await active_store.download(generation)
        restore_snapshot(ciphertext, resolved_key, db_path)
        write_boot_state(db_path, BootState(generation=generation, publish_enabled=True))
        logger.info(
            "event=snapshot_boot_restored generation=%s cipher_bytes=%d",
            generation,
            len(ciphertext),
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - the fail-closed boot boundary
        _mark_publish_disabled(db_path)
        logger.error("event=snapshot_boot_failed error=%s", type(exc).__name__)
        return 1


# ---------------------------------------------------------------------------
# One-shot publication (retention sweep path)
# ---------------------------------------------------------------------------


def _fatal_on_snapshot_upload(state: BootState) -> None:
    """Raise when the persisted state forbids publication."""
    if not state.publish_enabled:
        raise SnapshotStateError("snapshot publication is hard-disabled")


async def publish_snapshot_once(
    settings: Settings,
    *,
    store: SnapshotStore | None = None,
    key: bytes | None = None,
) -> int:
    """Build/encrypt/upload exactly once and advance the persisted generation.

    Used by the retention sweep (``purge_expired``), which must not report
    success while the durable object still holds purged content (C5).
    Raises :class:`SnapshotStateError` without boot state or with publication
    disabled, and store errors propagate — the caller fails closed.
    """
    if not settings.COACH_SNAPSHOT_BUCKET:
        raise SnapshotStateError("snapshot store is not configured")
    db_path = settings.COACH_DB_PATH
    state = read_boot_state(db_path)
    if state is None:
        raise SnapshotStateError("snapshot boot state missing or invalid")
    _fatal_on_snapshot_upload(state)
    active_store = store or GcsSnapshotClient(
        settings.COACH_SNAPSHOT_BUCKET, settings.COACH_SNAPSHOT_OBJECT
    )
    resolved_key = key if key is not None else resolve_data_encryption_key(settings)
    ciphertext = await asyncio.to_thread(encrypt_snapshot, db_path, resolved_key)
    generation = await active_store.upload(ciphertext, state.generation)
    write_boot_state(db_path, BootState(generation=generation, publish_enabled=True))
    return generation


# ---------------------------------------------------------------------------
# Runtime coordinator (debounced publisher + graceful flush)
# ---------------------------------------------------------------------------


class SnapshotCoordinator:
    """Debounced, generation-guarded snapshot publisher (MADR-0032 C5/C6).

    ``start()`` is fail-closed: with the bucket configured and no valid boot
    state the coordinator hard-disables publication and raises, so the app
    never becomes ready on a state it cannot trust. Post-boot upload failures
    degrade (MADR-0025 C7: chat keeps streaming) except ``412``, which
    permanently disables this stale instance's publication.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        store: SnapshotStore | None = None,
        key: bytes | None = None,
    ) -> None:
        self._settings = settings
        self._db_path = settings.COACH_DB_PATH
        self._store = store
        self._key = key
        self.enabled = bool(settings.COACH_SNAPSHOT_BUCKET)
        self.publish_enabled = False
        self.generation = 0
        self._dirty = False
        self._timer: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stopping = False

    def _active_store(self) -> SnapshotStore:
        if self._store is None:
            self._store = GcsSnapshotClient(
                self._settings.COACH_SNAPSHOT_BUCKET, self._settings.COACH_SNAPSHOT_OBJECT
            )
        return self._store

    async def start(self) -> None:
        """Load the boot state; refuse readiness when it is missing/invalid."""
        if not self.enabled:
            self.publish_enabled = False
            return
        state = read_boot_state(self._db_path)
        if state is None:
            self.publish_enabled = False
            raise SnapshotStateError("snapshot boot state missing or invalid")
        self.generation = state.generation
        self.publish_enabled = state.publish_enabled

    def mark_dirty(self) -> None:
        """Coalesce a committed write into the next debounced publication."""
        if not self.enabled or not self.publish_enabled or self._stopping:
            return
        self._dirty = True
        if self._timer is None or self._timer.done():
            self._timer = asyncio.get_running_loop().create_task(self._debounce_loop())

    async def _debounce_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.COACH_SNAPSHOT_DEBOUNCE_SECONDS)
            if not self._dirty:
                return
            self._dirty = False
            await self.publish_now()
            if not self._dirty or not self.publish_enabled:
                return

    async def publish_now(self) -> bool:
        """Publish one snapshot; ``False`` means nothing durable was written."""
        if not self.enabled or not self.publish_enabled:
            return False
        async with self._lock:
            if not self.enabled or not self.publish_enabled:
                return False
            try:
                key = (
                    self._key
                    if self._key is not None
                    else resolve_data_encryption_key(self._settings)
                )
                ciphertext = await asyncio.to_thread(encrypt_snapshot, self._db_path, key)
                generation = await self._active_store().upload(ciphertext, self.generation)
            except SnapshotConflict as exc:
                # Stale instance: never overwrite a newer writer (C6).
                self.publish_enabled = False
                self._dirty = False
                log_persistence_degrade(exc)
                return False
            except Exception as exc:  # noqa: BLE001 - post-boot degrade, never abort chat
                log_persistence_degrade(exc)
                return False
            self.generation = generation
            self._dirty = False
            try:
                write_boot_state(self._db_path, BootState(generation, True))
            except OSError:
                logger.warning("snapshot state persist failed (%s)", "OSError")
            return True

    async def stop(self) -> None:
        """Cancel the debounce timer and flush a final snapshot (SIGTERM)."""
        self._stopping = True
        timer = self._timer
        self._timer = None
        if timer is not None and not timer.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
        if self.enabled and self.publish_enabled:
            await self.publish_now()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m coach_web.snapshot boot-restore``."""
    parser = argparse.ArgumentParser(
        prog="python -m coach_web.snapshot",
        description=(
            "Durable encrypted GCS snapshot store (MADR-0032, #417): the "
            "fail-closed boot restore that runs before 'alembic upgrade head'."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "boot-restore",
        help=(
            "Restore the live encrypted snapshot (or confirm absence) before "
            "migrations; exits non-zero when the durable copy cannot be trusted."
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.command == "boot-restore":
        return asyncio.run(boot_restore(get_settings()))
    return 2


if __name__ == "__main__":
    sys.exit(main())
