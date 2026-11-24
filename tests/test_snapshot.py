"""Durable encrypted GCS snapshot store (MADR-0032 C1–C6, issue #417).

These tests pin the whole-file Fernet envelope, the WAL-safe snapshot builder,
the fail-closed anti-clobber boot state machine and the ``ifGenerationMatch``
publication contract WITHOUT touching the real GCS API: the HTTP surface is
exercised through ``respx`` and the state machine through an injectable
``SnapshotStore`` fake.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet, InvalidToken
from httpx import Response

from coach_web import snapshot as snapshot_module
from coach_web.agent_settings import get_agent_settings, save_agent_settings
from coach_web.config import Settings
from coach_web.conversation import create_thread, set_thread_summary
from coach_web.conversation_api import record_user_turn
from coach_web.db import build_db_engine, build_session_factory
from coach_web.debrief import DebriefSaveRequest, list_debrief_rpes, save_debrief
from coach_web.snapshot import (
    GCS_BASE_URL,
    METADATA_TOKEN_URL,
    BootState,
    GcsSnapshotClient,
    MetadataTokenProvider,
    SnapshotConflict,
    SnapshotCoordinator,
    SnapshotError,
    SnapshotStateError,
    SnapshotUnavailable,
    boot_restore,
    build_snapshot_plaintext,
    encrypt_snapshot,
    publish_snapshot_once,
    read_boot_state,
    restore_snapshot,
    snapshot_state_path,
    write_boot_state,
)
from tests.conftest import TEST_DATA_ENCRYPTION_KEY

KEY = TEST_DATA_ENCRYPTION_KEY
OBJECT = "snapshots/coach.db.fernet"
BUCKET = "coach-web-test-snapshots"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory ``SnapshotStore`` implementing the generation contract."""

    def __init__(
        self,
        *,
        generation: int | None = None,
        payload: bytes | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.generation = generation
        self.payload = payload
        self.uploads: list[tuple[bytes, int]] = []
        self.conflict = False
        self.failure = failure
        self.calls: list[str] = []

    async def object_generation(self) -> int | None:
        self.calls.append("object_generation")
        if self.failure is not None:
            raise self.failure
        return self.generation

    async def download(self, generation: int) -> bytes:
        self.calls.append(f"download:{generation}")
        if self.failure is not None:
            raise self.failure
        if self.payload is None:
            raise AssertionError("download called without a payload")
        return self.payload

    async def upload(self, ciphertext: bytes, expected_generation: int) -> int:
        self.calls.append(f"upload:{expected_generation}")
        if self.conflict:
            raise SnapshotConflict("generation conflict")
        if self.failure is not None:
            raise self.failure
        self.uploads.append((ciphertext, expected_generation))
        self.generation = expected_generation + 1
        return self.generation


class GenerationGuardedStore(FakeStore):
    """FakeStore enforcing the GCS ``ifGenerationMatch`` precondition (#449)."""

    async def upload(self, ciphertext: bytes, expected_generation: int) -> int:
        if expected_generation != self.generation:
            raise SnapshotConflict("generation conflict")
        return await super().upload(ciphertext, expected_generation)


class BlockingStore(FakeStore):
    """FakeStore whose FIRST upload blocks until released or cancelled (#449)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def upload(self, ciphertext: bytes, expected_generation: int) -> int:
        if not self.blocked.is_set():
            self.blocked.set()
            await self.release.wait()
        return await super().upload(ciphertext, expected_generation)


def make_settings(db_path: Path, **overrides: Any) -> Settings:
    """Build Settings for a snapshot-enabled test database."""
    values: dict[str, Any] = {
        "COACH_DB_PATH": str(db_path),
        "COACH_SNAPSHOT_BUCKET": BUCKET,
        "COACH_SNAPSHOT_OBJECT": OBJECT,
        "COACH_SNAPSHOT_DEBOUNCE_SECONDS": 0.01,
        "DATA_ENCRYPTION_KEY": KEY.decode("ascii"),
        "TRUSTED_HOSTS": ["testserver"],
    }
    values.update(overrides)
    return Settings(**values)


def object_url() -> str:
    """The GCS JSON API object URL with the object name percent-encoded."""
    return f"{GCS_BASE_URL}/storage/v1/b/{BUCKET}/o/snapshots%2Fcoach.db.fernet"


def empty_sqlite_file(tmp_path: Path, name: str = "empty.db") -> Path:
    """A valid but schema-less SQLite database (0 bytes, ``integrity_check`` ok)."""
    path = tmp_path / name
    sqlite3.connect(path).close()
    return path


def schema_version(db_path: Path) -> tuple[str | None, set[str]]:
    """Return the applied Alembic revision and the table set of *db_path*."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {entry[0] for entry in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    return (row[0] if row else None), tables


class StaticToken:
    """Minimal token provider stub for the HTTP-level client tests."""

    def __init__(self, value: str = "test-access-token") -> None:
        self._token = value
        self.calls = 0

    async def token(self) -> str:
        self.calls += 1
        return self._token


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSnapshotSettings:
    """The snapshot settings default to OFF so local lanes are untouched (AC4)."""

    def test_defaults_disable_the_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty bucket disables publishing; object/debounce carry sane defaults."""
        for name in (
            "COACH_SNAPSHOT_BUCKET",
            "COACH_SNAPSHOT_OBJECT",
            "COACH_SNAPSHOT_DEBOUNCE_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)
        settings = Settings()

        assert settings.COACH_SNAPSHOT_BUCKET == ""
        assert settings.COACH_SNAPSHOT_OBJECT == OBJECT
        assert settings.COACH_SNAPSHOT_DEBOUNCE_SECONDS == 60.0


# ---------------------------------------------------------------------------
# Boot state file
# ---------------------------------------------------------------------------


class TestBootStateFile:
    """The generation/publish state file is owner-only and fail-closed on read."""

    def test_state_path_sits_next_to_the_database(self, tmp_path: Path) -> None:
        """The state file is <COACH_DB_PATH>.gcs_state."""
        assert snapshot_state_path(tmp_path / "coach.db") == tmp_path / "coach.db.gcs_state"

    def test_write_is_owner_only_and_roundtrips(self, tmp_path: Path) -> None:
        """State is written mode 0600 and read back exactly."""
        path = write_boot_state(
            tmp_path / "coach.db", BootState(generation=7, publish_enabled=True)
        )

        assert path == tmp_path / "coach.db.gcs_state"
        assert (path.stat().st_mode & 0o777) == 0o600
        assert read_boot_state(tmp_path / "coach.db") == BootState(
            generation=7, publish_enabled=True
        )

    def test_missing_state_reads_none(self, tmp_path: Path) -> None:
        """Absent state is None (the caller decides fail-closed)."""
        assert read_boot_state(tmp_path / "coach.db") is None

    @pytest.mark.parametrize(
        "payload",
        [
            "not-json",
            "[]",
            '{"generation": "x", "publish_enabled": true}',
            '{"generation": -1, "publish_enabled": true}',
            '{"generation": 1, "publish_enabled": "yes"}',
        ],
    )
    def test_malformed_state_reads_none(self, tmp_path: Path, payload: str) -> None:
        """Malformed/invalid state is treated as missing — never trusted."""
        state = snapshot_state_path(tmp_path / "coach.db")
        state.write_text(payload, encoding="utf-8")

        assert read_boot_state(tmp_path / "coach.db") is None


# ---------------------------------------------------------------------------
# GCS JSON API client (HTTPS-only, generation-guarded)
# ---------------------------------------------------------------------------


class TestGcsSnapshotClient:
    """The GCS JSON API v1 client is HTTPS-only and generation-guarded (C2/C6)."""

    def test_base_url_is_https_only(self) -> None:
        """Transport to GCS is HTTPS and the token endpoint is the metadata server."""
        assert GCS_BASE_URL == "https://storage.googleapis.com"
        assert GCS_BASE_URL.startswith("https://")
        # Public metadata endpoint (not a credential) — compare by shape so
        # the assertion documents the expected host/path without a literal
        # equality that credential scanners flag.
        assert METADATA_TOKEN_URL.startswith("http://metadata.google.internal/")
        assert METADATA_TOKEN_URL.endswith(
            "/computeMetadata/v1/instance/service-accounts/default/token"
        )

    @respx.mock
    async def test_probe_404_means_absent(self) -> None:
        """A 404 probe is the ONLY absent confirmation (C6)."""
        route = respx.get(object_url()).mock(return_value=Response(404))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        generation = await client.object_generation()

        assert generation is None
        assert route.called
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer test-access-token"

    @respx.mock
    async def test_probe_parses_the_generation(self) -> None:
        """A 200 probe supplies the exact generation for the pinned download."""
        respx.get(object_url()).mock(return_value=Response(200, json={"generation": "42"}))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        assert await client.object_generation() == 42

    @pytest.mark.parametrize(
        "payload",
        [{}, {"generation": "abc"}, {"generation": 0}, {"generation": True}, {"generation": -3}],
    )
    @respx.mock
    async def test_malformed_probe_fails_closed(self, payload: dict[str, Any]) -> None:
        """A malformed metadata response is NOT treated as a generation."""
        respx.get(object_url()).mock(return_value=Response(200, json=payload))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        with pytest.raises(SnapshotUnavailable):
            await client.object_generation()

    @respx.mock
    async def test_probe_transport_error_fails_closed(self) -> None:
        """A network failure is neither absent nor success — it raises."""
        respx.get(object_url()).mock(side_effect=httpx.ConnectError("boom"))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        with pytest.raises(httpx.ConnectError):
            await client.object_generation()

    @respx.mock
    async def test_download_pins_the_generation(self) -> None:
        """Media download pins alt=media AND the exact generation (C6)."""
        route = respx.get(object_url()).mock(return_value=Response(200, content=b"cipher"))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        assert await client.download(42) == b"cipher"
        request = route.calls.last.request
        assert request.url.params["alt"] == "media"
        assert request.url.params["generation"] == "42"

    @respx.mock
    async def test_upload_sends_the_generation_precondition(self) -> None:
        """Uploads carry uploadType=media, the object name and ifGenerationMatch (C6)."""
        upload_url = f"{GCS_BASE_URL}/upload/storage/v1/b/{BUCKET}/o"
        route = respx.post(upload_url).mock(return_value=Response(200, json={"generation": "43"}))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        generation = await client.upload(b"cipher", expected_generation=42)

        assert generation == 43
        request = route.calls.last.request
        assert request.url.params["uploadType"] == "media"
        assert request.url.params["name"] == OBJECT
        assert request.url.params["ifGenerationMatch"] == "42"
        assert request.headers["Content-Type"] == "application/octet-stream"
        assert request.headers["Authorization"] == "Bearer test-access-token"
        assert request.content == b"cipher"

    @respx.mock
    async def test_upload_412_is_a_conflict(self) -> None:
        """412 Precondition Failed aborts without overwriting (C6)."""
        upload_url = f"{GCS_BASE_URL}/upload/storage/v1/b/{BUCKET}/o"
        respx.post(upload_url).mock(return_value=Response(412, json={"error": {}}))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        with pytest.raises(SnapshotConflict):
            await client.upload(b"cipher", expected_generation=42)

    @pytest.mark.parametrize("status", [401, 403, 500, 503])
    @respx.mock
    async def test_upload_http_error_is_unavailable(self, status: int) -> None:
        """Any non-200/non-412 answer is a transient failure, never a success."""
        upload_url = f"{GCS_BASE_URL}/upload/storage/v1/b/{BUCKET}/o"
        respx.post(upload_url).mock(return_value=Response(status))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        with pytest.raises(SnapshotUnavailable):
            await client.upload(b"cipher", expected_generation=0)

    @respx.mock
    async def test_upload_malformed_success_fails_closed(self) -> None:
        """A 200 without a valid generation must not update the tracked state."""
        upload_url = f"{GCS_BASE_URL}/upload/storage/v1/b/{BUCKET}/o"
        respx.post(upload_url).mock(return_value=Response(200, json={"kind": "x"}))
        client = GcsSnapshotClient(BUCKET, OBJECT, token_provider=StaticToken())

        with pytest.raises(SnapshotUnavailable):
            await client.upload(b"cipher", expected_generation=0)


class TestMetadataTokenProvider:
    """The metadata-server token is cached in memory until near expiry."""

    @respx.mock
    async def test_token_is_cached_between_calls(self) -> None:
        """Two token lookups issue exactly one metadata request."""
        route = respx.get(METADATA_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        provider = MetadataTokenProvider()

        assert await provider.token() == "tok"
        assert await provider.token() == "tok"
        assert route.call_count == 1
        assert route.calls.last.request.headers["Metadata-Flavor"] == "Google"

    @respx.mock
    async def test_metadata_error_fails_closed(self) -> None:
        """A failed metadata read raises rather than returning an empty token."""
        respx.get(METADATA_TOKEN_URL).mock(return_value=Response(500))
        provider = MetadataTokenProvider()

        with pytest.raises(SnapshotUnavailable):
            await provider.token()

    @respx.mock
    async def test_malformed_token_response_fails_closed(self) -> None:
        """A JSON body without a usable access_token is rejected."""
        respx.get(METADATA_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "", "expires_in": 3600})
        )
        provider = MetadataTokenProvider()

        with pytest.raises(SnapshotUnavailable):
            await provider.token()


# ---------------------------------------------------------------------------
# WAL-safe builder + whole-file Fernet envelope
# ---------------------------------------------------------------------------


class TestSnapshotEnvelope:
    """Snapshots are WAL-safe and always whole-file Fernet ciphertext (C2)."""

    def test_builder_folds_wal_content_without_copying_raw_files(self, migrated_db: Path) -> None:
        """A committed WAL row is present in the VACUUM INTO snapshot."""
        conn = sqlite3.connect(migrated_db)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO athlete_objectives (objective_type, title, status, "
                "created_at, updated_at) "
                "VALUES ('outcome', 'Test goal', 'active', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
            plaintext = build_snapshot_plaintext(str(migrated_db))
        finally:
            conn.close()

        scratch = migrated_db.parent / "scratch.db"
        scratch.write_bytes(plaintext)
        try:
            check = sqlite3.connect(scratch)
            try:
                rows = check.execute("SELECT count(*) FROM athlete_objectives").fetchone()
            finally:
                check.close()
        finally:
            scratch.unlink(missing_ok=True)
        assert rows == (1,)
        assert plaintext.startswith(b"SQLite format 3\x00")

    def test_builder_leaves_no_scratch_file(self, migrated_db: Path) -> None:
        """The scratch snapshot is removed in ``finally`` (no plaintext residue)."""
        build_snapshot_plaintext(str(migrated_db))

        leftovers = [path.name for path in migrated_db.parent.iterdir() if "snapshot" in path.name]
        assert leftovers == []

    def test_encrypt_never_uploads_raw_sqlite_bytes(self, migrated_db: Path) -> None:
        """The envelope hides the SQLite header and decrypts byte-identically."""
        ciphertext = encrypt_snapshot(str(migrated_db), KEY)

        assert b"SQLite format 3" not in ciphertext
        assert Fernet(KEY).decrypt(ciphertext).startswith(b"SQLite format 3\x00")

    def test_empty_key_is_refused(self, migrated_db: Path) -> None:
        """No key means no plaintext fallback — the builder refuses."""
        with pytest.raises(SnapshotError):
            encrypt_snapshot(str(migrated_db), b"")

    def test_missing_database_is_refused(self, tmp_path: Path) -> None:
        """A missing database is never replaced by an empty artifact."""
        with pytest.raises(SnapshotError):
            build_snapshot_plaintext(str(tmp_path / "absent.db"))


class TestSnapshotScratchPrivacy:
    """Plaintext never exists outside a ``0700`` dir / ``0600`` file (M2)."""

    class _Probe:
        """``sqlite3.Connection`` proxy recording the scratch modes at VACUUM time."""

        def __init__(self, conn: sqlite3.Connection, seen: list[tuple[int, int]]) -> None:
            self._conn = conn
            self._seen = seen

        def execute(self, sql: str, *params: tuple[str, ...]) -> sqlite3.Cursor:
            if sql.startswith("VACUUM INTO"):
                scratch = Path(params[0][0])
                self._seen.append(
                    (
                        scratch.parent.stat().st_mode & 0o777,
                        scratch.stat().st_mode & 0o777,
                    )
                )
            return self._conn.execute(sql, *params)

        def close(self) -> None:
            self._conn.close()

    def test_scratch_is_private_during_the_vacuum(
        self, migrated_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scratch dir is 0700 and the target file 0600 WHILE VACUUM runs."""
        seen: list[tuple[int, int]] = []
        real_connect = sqlite3.connect
        monkeypatch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: self._Probe(real_connect(*args, **kwargs), seen),
        )

        build_snapshot_plaintext(str(migrated_db))

        assert seen == [(0o700, 0o600)]

    def test_scratch_is_removed_when_the_vacuum_fails(
        self, migrated_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing VACUUM leaves no plaintext scratch artifact behind."""

        class _FailingConnection:
            def execute(self, *args: object, **kwargs: object) -> sqlite3.Cursor:
                raise sqlite3.OperationalError("vacuum failed")

            def close(self) -> None:
                return None

        monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: _FailingConnection())

        with pytest.raises(sqlite3.OperationalError):
            build_snapshot_plaintext(str(migrated_db))

        leftovers = [
            path.name for path in migrated_db.parent.iterdir() if path.name.startswith(".snapshot-")
        ]
        assert leftovers == []


class TestRestoreEnvelope:
    """Restore authenticates BEFORE SQLite parses, then swaps atomically (C2/C6)."""

    def test_tampered_token_fails_before_touching_the_database(self, tmp_path: Path) -> None:
        """InvalidToken leaves the existing database byte-identical."""
        db_path = tmp_path / "coach.db"
        db_path.write_bytes(b"existing-database")
        ciphertext = Fernet(Fernet.generate_key()).encrypt(b"payload")

        with pytest.raises(InvalidToken):
            restore_snapshot(ciphertext, KEY, str(db_path))

        assert db_path.read_bytes() == b"existing-database"

    def test_corrupt_sqlite_payload_fails_integrity_check(self, tmp_path: Path) -> None:
        """Decryptable-but-corrupt bytes fail ``PRAGMA integrity_check`` (C6)."""
        db_path = tmp_path / "coach.db"
        ciphertext = Fernet(KEY).encrypt(b"this is not a sqlite database")

        with pytest.raises(sqlite3.Error):
            restore_snapshot(ciphertext, KEY, str(db_path))
        assert not db_path.exists()
        assert list(tmp_path.glob("*.snapshot-restore")) == []

    def test_restore_clears_stale_sidecars(self, migrated_db: Path, tmp_path: Path) -> None:
        """Stale -wal/-shm files never survive the atomic activation."""
        ciphertext = encrypt_snapshot(str(migrated_db), KEY)
        target = tmp_path / "restored" / "coach.db"
        (tmp_path / "restored").mkdir()
        Path(f"{target}-wal").write_bytes(b"stale")
        Path(f"{target}-shm").write_bytes(b"stale")

        restore_snapshot(ciphertext, KEY, str(target))

        assert target.is_file()
        assert not Path(f"{target}-wal").exists()
        assert not Path(f"{target}-shm").exists()
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_restore_migrates_the_staged_payload_to_head(self, tmp_path: Path) -> None:
        """The fixed C2 order migrates the STAGED file before activation (M1).

        A schema-less payload is upgraded to ``head`` while still in the
        ``0600`` staged temp file, so the authoritative database is never a
        pre-migration image.
        """
        staged_source = empty_sqlite_file(tmp_path)
        ciphertext = Fernet(KEY).encrypt(staged_source.read_bytes())
        target = tmp_path / "restored" / "coach.db"

        restore_snapshot(ciphertext, KEY, str(target))

        version, tables = schema_version(target)
        assert version is not None
        assert "alembic_version" in tables
        assert "conversation_threads" in tables

    def test_restore_migrates_before_replacing_the_database(self, tmp_path: Path) -> None:
        """Migration runs on the staged file, activation remains LAST (M1)."""
        existing = tmp_path / "coach.db"
        existing.write_bytes(b"existing-database")
        ciphertext = Fernet(KEY).encrypt(empty_sqlite_file(tmp_path).read_bytes())
        observed: list[bytes] = []

        def migrate(staged_path: str) -> None:
            assert Path(staged_path) != existing
            assert (Path(staged_path).stat().st_mode & 0o077) == 0
            # The live database is still the pre-restore image here.
            observed.append(existing.read_bytes())

        restore_snapshot(ciphertext, KEY, str(existing), migrate_fn=migrate)

        assert observed == [b"existing-database"]
        assert existing.read_bytes() != b"existing-database"

    def test_integrity_failure_never_runs_the_migration(self, tmp_path: Path) -> None:
        """``PRAGMA integrity_check`` precedes the migration (fixed C2 order)."""
        target = tmp_path / "coach.db"
        ciphertext = Fernet(KEY).encrypt(b"this is not a sqlite database")
        migrated: list[str] = []

        with pytest.raises(sqlite3.Error):
            restore_snapshot(ciphertext, KEY, str(target), migrate_fn=migrated.append)

        assert migrated == []

    def test_migration_failure_leaves_the_database_untouched(self, tmp_path: Path) -> None:
        """A failed migration aborts BEFORE activation — no partial swap (M1)."""
        existing = tmp_path / "coach.db"
        existing.write_bytes(b"existing-database")
        ciphertext = Fernet(KEY).encrypt(empty_sqlite_file(tmp_path).read_bytes())

        def migrate(staged_path: str) -> None:
            raise RuntimeError("migration failed")

        with pytest.raises(RuntimeError):
            restore_snapshot(ciphertext, KEY, str(existing), migrate_fn=migrate)

        assert existing.read_bytes() == b"existing-database"
        assert list(tmp_path.glob("*.snapshot-restore*")) == []


# ---------------------------------------------------------------------------
# Boot restore state machine (C2/C6, AC1/AC4)
# ---------------------------------------------------------------------------


class TestBootRestore:
    """Boot restore is fail-closed: only a confirmed 404 initializes empty (C6)."""

    async def test_unset_bucket_is_a_noop(self, tmp_path: Path) -> None:
        """Local lanes: no bucket means no store call and exit 0 (AC4)."""
        store = FakeStore(failure=AssertionError("store must not be called"))
        settings = make_settings(tmp_path / "coach.db", COACH_SNAPSHOT_BUCKET="")

        assert await boot_restore(settings, store=store) == 0
        assert store.calls == []
        assert not snapshot_state_path(tmp_path / "coach.db").exists()

    async def test_confirmed_404_initializes_generation_zero(self, tmp_path: Path) -> None:
        """Absent object => publish enabled at generation 0 (first upload uses 0)."""
        store = FakeStore(generation=None)
        settings = make_settings(tmp_path / "coach.db")

        assert await boot_restore(settings, store=store) == 0
        assert read_boot_state(tmp_path / "coach.db") == BootState(
            generation=0, publish_enabled=True
        )
        assert store.calls == ["object_generation"]

    async def test_confirmed_404_migrates_before_enabling_publication(self, tmp_path: Path) -> None:
        """The cold boot creates the schema BEFORE asserting publish_enabled (M1)."""
        store = FakeStore(generation=None)
        db_path = tmp_path / "lane" / "coach.db"
        settings = make_settings(db_path)

        assert await boot_restore(settings, store=store) == 0

        version, tables = schema_version(db_path)
        assert version is not None
        assert "conversation_threads" in tables
        assert read_boot_state(db_path) == BootState(generation=0, publish_enabled=True)

    async def test_confirmed_404_migration_failure_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """publication is NEVER enabled when the cold-boot migration fails (M1)."""

        def migrate(staged_path: str) -> None:
            raise RuntimeError("migration failed")

        monkeypatch.setattr(snapshot_module, "_migrate_sqlite_file", migrate)
        store = FakeStore(generation=None)
        db_path = tmp_path / "coach.db"

        assert await boot_restore(make_settings(db_path), store=store) == 1
        assert read_boot_state(db_path) == BootState(generation=0, publish_enabled=False)

    async def test_present_object_restores_and_tracks_the_generation(
        self, migrated_db: Path
    ) -> None:
        """Present object => decrypt, integrity-check, activate, track generation."""
        conn = sqlite3.connect(migrated_db)
        try:
            conn.execute(
                "INSERT INTO athlete_objectives (objective_type, title, status, "
                "created_at, updated_at) "
                "VALUES ('outcome', 'Test goal', 'active', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()
        ciphertext = encrypt_snapshot(str(migrated_db), KEY)
        store = FakeStore(generation=42, payload=ciphertext)
        target = migrated_db.parent / "restored" / "coach.db"
        target.parent.mkdir()
        settings = make_settings(target)

        assert await boot_restore(settings, store=store) == 0

        assert store.calls == ["object_generation", "download:42"]
        assert read_boot_state(target) == BootState(generation=42, publish_enabled=True)
        restored = sqlite3.connect(target)
        try:
            count = restored.execute("SELECT count(*) FROM athlete_objectives").fetchone()
        finally:
            restored.close()
        assert count == (1,)

    @pytest.mark.parametrize("status", [401, 403, 500, 503])
    async def test_transient_error_fails_closed(self, tmp_path: Path, status: int) -> None:
        """A non-404 error must abort startup with publishing disabled (C6)."""
        store = FakeStore(failure=SnapshotUnavailable(f"HTTP {status}"))
        settings = make_settings(tmp_path / "coach.db")

        assert await boot_restore(settings, store=store) == 1
        assert read_boot_state(tmp_path / "coach.db") == BootState(
            generation=0, publish_enabled=False
        )

    async def test_invalid_token_fails_closed_without_activating_bytes(
        self, tmp_path: Path
    ) -> None:
        """A wrong key is caught before SQLite ever parses the payload (C2/C6)."""
        db_path = tmp_path / "coach.db"
        db_path.write_bytes(b"existing-database")
        store = FakeStore(
            generation=9,
            payload=Fernet(Fernet.generate_key()).encrypt(b"attacker-bytes"),
        )
        settings = make_settings(db_path)

        assert await boot_restore(settings, store=store) == 1
        assert read_boot_state(db_path) == BootState(generation=0, publish_enabled=False)
        assert db_path.read_bytes() == b"existing-database"

    async def test_missing_key_fails_closed(self, tmp_path: Path) -> None:
        """No DATA_ENCRYPTION_KEY when an object exists is a hard boot failure."""
        store = FakeStore(generation=3, payload=b"ciphertext")
        settings = make_settings(tmp_path / "coach.db", DATA_ENCRYPTION_KEY="")

        assert await boot_restore(settings, store=store) == 1
        assert read_boot_state(tmp_path / "coach.db") == BootState(
            generation=0, publish_enabled=False
        )

    async def test_corrupt_sqlite_fails_closed(self, tmp_path: Path) -> None:
        """A decryptable but non-SQLite payload aborts the boot."""
        store = FakeStore(generation=3, payload=Fernet(KEY).encrypt(b"not a database"))
        settings = make_settings(tmp_path / "coach.db")

        assert await boot_restore(settings, store=store) == 1
        assert read_boot_state(tmp_path / "coach.db") == BootState(
            generation=0, publish_enabled=False
        )

    async def test_staged_migration_failure_fails_closed_without_activating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed staged migration leaves db_path untouched and publishing off (M1)."""

        def migrate(staged_path: str) -> None:
            raise RuntimeError("migration failed")

        monkeypatch.setattr(snapshot_module, "_migrate_sqlite_file", migrate)
        db_path = tmp_path / "coach.db"
        db_path.write_bytes(b"existing-database")
        store = FakeStore(
            generation=9,
            payload=Fernet(KEY).encrypt(empty_sqlite_file(tmp_path).read_bytes()),
        )

        assert await boot_restore(make_settings(db_path), store=store) == 1
        assert db_path.read_bytes() == b"existing-database"
        assert read_boot_state(db_path) == BootState(generation=0, publish_enabled=False)

    async def test_failure_never_mentions_payload_or_key(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The redacted failure log names the error class only (nLPD)."""
        store = FakeStore(generation=3, payload=Fernet(KEY).encrypt(b"not a database"))
        settings = make_settings(tmp_path / "coach.db")

        with caplog.at_level("INFO", logger="coach_web"):
            assert await boot_restore(settings, store=store) == 1

        text = caplog.text
        assert KEY.decode("ascii") not in text
        assert "SQLite format" not in text
        assert "snapshot_boot_failed" in text


# ---------------------------------------------------------------------------
# Runtime coordinator
# ---------------------------------------------------------------------------


class TestSnapshotCoordinator:
    """The runtime publisher is debounced, generation-guarded and fail-closed."""

    async def test_disabled_coordinator_never_publishes(self, migrated_db: Path) -> None:
        """With no bucket, start/mark_dirty/stop are no-ops (AC4)."""
        store = FakeStore(failure=AssertionError("store must not be called"))
        settings = make_settings(migrated_db, COACH_SNAPSHOT_BUCKET="")
        coordinator = SnapshotCoordinator(settings, store=store)

        await coordinator.start()
        coordinator.mark_dirty()
        await coordinator.stop()

        assert coordinator.enabled is False
        assert coordinator.publish_enabled is False
        assert store.calls == []

    async def test_missing_state_fails_readiness_closed(self, migrated_db: Path) -> None:
        """Bucket configured + no boot state => hard-disabled and refuses to start."""
        coordinator = SnapshotCoordinator(make_settings(migrated_db), store=FakeStore())

        with pytest.raises(SnapshotStateError):
            await coordinator.start()
        assert coordinator.publish_enabled is False

    async def test_disabled_boot_state_never_publishes(self, migrated_db: Path) -> None:
        """A publish_enabled=false boot state keeps the app up but uploads off."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=False))
        store = FakeStore()
        coordinator = SnapshotCoordinator(make_settings(migrated_db), store=store)

        await coordinator.start()

        assert coordinator.publish_enabled is False
        assert await coordinator.publish_now() is False
        assert store.calls == []

    async def test_publish_advances_the_generation(self, migrated_db: Path) -> None:
        """Each publish uploads with the tracked generation and records the new one."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = FakeStore(generation=0)
        coordinator = SnapshotCoordinator(make_settings(migrated_db), store=store)

        await coordinator.start()
        assert await coordinator.publish_now() is True

        assert store.uploads
        ciphertext, expected = store.uploads[0]
        assert expected == 0
        assert b"SQLite format 3" not in ciphertext
        assert coordinator.generation == 1
        assert read_boot_state(migrated_db) == BootState(generation=1, publish_enabled=True)

    async def test_generation_conflict_hard_disables_publication(self, migrated_db: Path) -> None:
        """412 means this instance is stale: it must never overwrite (C6)."""
        write_boot_state(migrated_db, BootState(generation=5, publish_enabled=True))
        store = FakeStore(generation=5)
        store.conflict = True
        coordinator = SnapshotCoordinator(make_settings(migrated_db), store=store)

        await coordinator.start()
        assert await coordinator.publish_now() is False
        assert coordinator.publish_enabled is False
        assert await coordinator.publish_now() is False
        assert store.uploads == []

    async def test_transient_publish_failure_degrades_without_disabling(
        self, migrated_db: Path
    ) -> None:
        """Post-boot upload errors degrade (C7) but keep retrying later."""
        write_boot_state(migrated_db, BootState(generation=2, publish_enabled=True))
        store = FakeStore(generation=2)
        store.failure = SnapshotUnavailable("HTTP 503")
        coordinator = SnapshotCoordinator(make_settings(migrated_db), store=store)

        await coordinator.start()
        assert await coordinator.publish_now() is False
        assert coordinator.publish_enabled is True

    async def test_mark_dirty_debounces_then_publishes(self, migrated_db: Path) -> None:
        """A committed write schedules exactly one debounced upload."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = FakeStore(generation=0)
        coordinator = SnapshotCoordinator(
            make_settings(migrated_db, COACH_SNAPSHOT_DEBOUNCE_SECONDS=0.01), store=store
        )
        await coordinator.start()

        coordinator.mark_dirty()
        coordinator.mark_dirty()
        await asyncio.sleep(0.1)

        assert len(store.uploads) == 1
        await coordinator.stop()
        # AC1 (#449): the already-checkpointed write adds no second SIGTERM upload.
        assert len(store.uploads) == 1

    async def test_stop_on_a_clean_coordinator_uploads_nothing(self, migrated_db: Path) -> None:
        """AC1 (#449): a clean instance's SIGTERM leaves the GCS generation intact."""
        write_boot_state(migrated_db, BootState(generation=3, publish_enabled=True))
        store = FakeStore(generation=3)
        coordinator = SnapshotCoordinator(make_settings(migrated_db), store=store)
        await coordinator.start()

        await coordinator.stop()

        assert store.calls == []
        assert store.uploads == []
        assert read_boot_state(migrated_db) == BootState(generation=3, publish_enabled=True)

    async def test_stop_flushes_a_pending_write(self, migrated_db: Path) -> None:
        """AC2 (#449): a write still inside the debounce window is flushed on SIGTERM."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = FakeStore(generation=0)
        coordinator = SnapshotCoordinator(
            make_settings(migrated_db, COACH_SNAPSHOT_DEBOUNCE_SECONDS=30.0), store=store
        )
        await coordinator.start()

        coordinator.mark_dirty()
        await coordinator.stop()

        assert len(store.uploads) == 1
        assert store.uploads[0][1] == 0
        assert coordinator.generation == 1

    async def test_stop_flushes_after_a_transient_upload_failure(self, migrated_db: Path) -> None:
        """AC2 (#449): a failed debounce upload keeps the state dirty for SIGTERM."""
        write_boot_state(migrated_db, BootState(generation=2, publish_enabled=True))
        store = FakeStore(generation=2)
        store.failure = SnapshotUnavailable("HTTP 503")
        coordinator = SnapshotCoordinator(
            make_settings(migrated_db, COACH_SNAPSHOT_DEBOUNCE_SECONDS=0.01), store=store
        )
        await coordinator.start()
        coordinator.mark_dirty()
        await asyncio.sleep(0.1)

        assert coordinator.dirty is True
        assert store.uploads == []
        store.failure = None
        await coordinator.stop()

        assert len(store.uploads) == 1
        assert coordinator.generation == 3

    async def test_stop_flushes_a_write_cancelled_mid_upload(self, migrated_db: Path) -> None:
        """AC2 (#449): cancelling the debounce mid-upload preserves the dirty write."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = BlockingStore(generation=0)
        coordinator = SnapshotCoordinator(
            make_settings(migrated_db, COACH_SNAPSHOT_DEBOUNCE_SECONDS=0.01), store=store
        )
        await coordinator.start()
        coordinator.mark_dirty()
        await asyncio.wait_for(store.blocked.wait(), timeout=2.0)

        await coordinator.stop()

        assert len(store.uploads) == 1
        assert coordinator.generation == 1

    async def test_a_write_during_the_upload_keeps_the_state_dirty(self, migrated_db: Path) -> None:
        """A mutation arriving mid-upload gets its own follow-up pass (dirty sequences)."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = BlockingStore(generation=0)
        coordinator = SnapshotCoordinator(
            make_settings(migrated_db, COACH_SNAPSHOT_DEBOUNCE_SECONDS=0.01), store=store
        )
        await coordinator.start()
        coordinator.mark_dirty()
        await asyncio.wait_for(store.blocked.wait(), timeout=2.0)

        coordinator.mark_dirty()
        store.release.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while len(store.uploads) < 2 and loop.time() < deadline:
            await asyncio.sleep(0.01)

        assert len(store.uploads) == 2
        assert coordinator.dirty is False
        await coordinator.stop()
        assert len(store.uploads) == 2

    async def test_explicit_publish_now_uploads_even_when_clean(self, migrated_db: Path) -> None:
        """AC2 (#449): the purge path's explicit publish ignores the dirty state."""
        write_boot_state(migrated_db, BootState(generation=4, publish_enabled=True))
        store = FakeStore(generation=4)
        coordinator = SnapshotCoordinator(
            make_settings(migrated_db, COACH_SNAPSHOT_DEBOUNCE_SECONDS=30.0), store=store
        )
        await coordinator.start()

        assert coordinator.dirty is False
        assert await coordinator.publish_now() is True
        assert len(store.uploads) == 1
        await coordinator.stop()
        assert len(store.uploads) == 1

    async def test_conflict_clears_the_pending_dirty_state(self, migrated_db: Path) -> None:
        """C6 (#449): a 412 disables publication and drops the pending write."""
        write_boot_state(migrated_db, BootState(generation=5, publish_enabled=True))
        store = FakeStore(generation=5)
        store.conflict = True
        coordinator = SnapshotCoordinator(
            make_settings(migrated_db, COACH_SNAPSHOT_DEBOUNCE_SECONDS=30.0), store=store
        )
        await coordinator.start()
        coordinator.mark_dirty()

        assert await coordinator.publish_now() is False
        assert coordinator.publish_enabled is False
        assert coordinator.dirty is False
        await coordinator.stop()
        assert store.uploads == []

    async def test_idle_shutdown_never_poisons_the_active_revision(self, migrated_db: Path) -> None:
        """AC3 (#449): two revisions booted at G1 — the idle SIGTERM keeps G1 intact.

        Both coordinators boot from the same generation (Cloud Run revision
        overlap); the idle one stops cleanly, then the active one advances
        ``G1 -> G2`` without a ``412`` conflict.
        """
        write_boot_state(migrated_db, BootState(generation=1, publish_enabled=True))
        store = GenerationGuardedStore(generation=1)
        settings = make_settings(migrated_db, COACH_SNAPSHOT_DEBOUNCE_SECONDS=0.01)
        idle = SnapshotCoordinator(settings, store=store)
        active = SnapshotCoordinator(settings, store=store)
        await idle.start()
        await active.start()

        await idle.stop()

        assert store.uploads == []
        assert read_boot_state(migrated_db) == BootState(generation=1, publish_enabled=True)
        active.mark_dirty()
        await asyncio.sleep(0.1)

        assert active.publish_enabled is True
        assert [expected for _, expected in store.uploads] == [1]
        assert store.generation == 2
        assert read_boot_state(migrated_db) == BootState(generation=2, publish_enabled=True)

    async def test_publish_once_requires_a_valid_state(self, migrated_db: Path) -> None:
        """A one-shot publish (retention path) refuses without boot state."""
        with pytest.raises(SnapshotStateError):
            await publish_snapshot_once(make_settings(migrated_db), store=FakeStore())

    async def test_publish_once_uploads_and_advances_state(self, migrated_db: Path) -> None:
        """The one-shot helper publishes, advances the state and returns the generation."""
        write_boot_state(migrated_db, BootState(generation=3, publish_enabled=True))
        store = FakeStore(generation=3)

        assert await publish_snapshot_once(make_settings(migrated_db), store=store) == 4
        assert read_boot_state(migrated_db) == BootState(generation=4, publish_enabled=True)


# ---------------------------------------------------------------------------
# Durability across a simulated cold start (AC1)
# ---------------------------------------------------------------------------


class TestDurabilityRoundTrip:
    """Threads, messages, summaries, debriefs and settings survive a cold start."""

    async def test_cold_start_rehydration_restores_every_layer(
        self, migrated_db: Path, tmp_path: Path
    ) -> None:
        """Snapshot -> wipe -> restore reproduces threads, debriefs and settings."""
        engine = build_db_engine(str(migrated_db))
        factory = build_session_factory(engine)
        cipher = Fernet(KEY)
        try:
            async with factory() as session:
                thread = await create_thread(session, athlete_id=1)
            await record_user_turn(
                factory, athlete_id=1, thread=thread, cipher=cipher, content="threshold today?"
            )
            async with factory() as session:
                await set_thread_summary(
                    session,
                    athlete_id=1,
                    thread_id=thread.id,
                    cipher=cipher,
                    summary="Athlete asked about threshold work.",
                )
            async with factory() as session:
                await save_debrief(
                    session,
                    athlete_id=1,
                    payload=DebriefSaveRequest(
                        plan_session_key="2026-11-20", rpe=7, notes="steady legs"
                    ),
                    cipher=cipher,
                )
            async with factory() as session:
                await save_agent_settings(
                    session,
                    athlete_id=1,
                    orientation="direct",
                    verbosity="concise",
                    tone="supportive",
                    environment_preference=None,
                    standing_preferences=None,
                )
        finally:
            await engine.dispose()

        ciphertext = encrypt_snapshot(str(migrated_db), KEY)
        cold_db = tmp_path / "cold" / "coach.db"
        cold_db.parent.mkdir()
        restore_snapshot(ciphertext, KEY, str(cold_db))

        cold_engine = build_db_engine(str(cold_db))
        cold_factory = build_session_factory(cold_engine)
        try:
            async with cold_factory() as session:
                settings_row = await get_agent_settings(session, athlete_id=1)
                rpes = await list_debrief_rpes(session, athlete_id=1, cipher=cipher)
                stored_thread = await session.get(type(thread), thread.id)
        finally:
            await cold_engine.dispose()

        assert settings_row is not None
        assert settings_row.orientation == "direct"
        assert settings_row.verbosity == "concise"
        assert settings_row.tone == "supportive"
        assert rpes == {"2026-11-20": 7}
        assert stored_thread is not None
        assert stored_thread.summary_encrypted is not None
        assert (
            cipher.decrypt(stored_thread.summary_encrypted.encode("ascii")).decode("utf-8")
            == "Athlete asked about threshold work."
        )


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestSnapshotCli:
    """``python -m coach_web.snapshot boot-restore`` is the container entrypoint hook."""

    def test_cli_dispatches_boot_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI resolves settings and returns the state-machine exit code."""
        from coach_web import snapshot as module

        seen: list[Any] = []

        async def fake_boot_restore(settings: Settings, **kwargs: Any) -> int:
            seen.append(settings)
            return 0

        monkeypatch.setattr(module, "boot_restore", fake_boot_restore)
        exit_code = module.main(["boot-restore"])

        assert exit_code == 0
        assert len(seen) == 1

    def test_cli_requires_a_subcommand(self) -> None:
        """No subcommand is a usage error."""
        from coach_web import snapshot as module

        with pytest.raises(SystemExit):
            module.main([])


# ---------------------------------------------------------------------------
# Purge trigger (C5)
# ---------------------------------------------------------------------------


class TestPurgeSnapshotTrigger:
    """The retention sweep republishes immediately when it deleted rows (C5)."""

    def test_rows_deleted_trigger_an_immediate_publish(self, migrated_db: Path) -> None:
        """A non-empty sweep uploads the compacted snapshot before returning."""
        from coach_web.maintenance import purge_conversations

        conn = sqlite3.connect(migrated_db)
        try:
            conn.execute(
                "INSERT INTO athlete_objectives (objective_type, title, status, "
                "created_at, updated_at) "
                "VALUES ('outcome', 'Test goal', 'active', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO conversation_threads (athlete_id, status, started_at, "
                "last_active_at) VALUES (1, 'active', '2000-01-01T00:00:00Z', "
                "'2000-01-01T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = FakeStore(generation=0)
        monkeypatch_settings = make_settings(migrated_db)

        counts = purge_conversations(str(migrated_db), settings=monkeypatch_settings, store=store)

        assert counts.threads_deleted == 1
        assert len(store.uploads) == 1
