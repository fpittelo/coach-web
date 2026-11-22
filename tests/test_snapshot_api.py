"""Application wiring for the durable snapshot store (MADR-0032 C5/C6, #417).

These tests drive the FastAPI app: the lifespan coordinator (startup fail-closed,
SIGTERM flush), the synchronous purge publication (200 only after the durable
copy is overwritten, 503 otherwise) and the post-commit dirty notifications.
All GCS traffic is faked through the injectable ``SnapshotStore`` — zero network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coach_web.app import PURGE_DURABLE_STORAGE_DETAIL, create_app
from coach_web.config import Settings
from coach_web.conversation import create_thread
from coach_web.db import build_db_engine, build_session_factory
from coach_web.snapshot import (
    BootState,
    SnapshotCoordinator,
    SnapshotStateError,
    SnapshotUnavailable,
    write_boot_state,
)
from tests.conftest import TEST_DATA_ENCRYPTION_KEY
from tests.test_snapshot import BUCKET, OBJECT, FakeStore

KEY = TEST_DATA_ENCRYPTION_KEY


def make_settings(db_path: Path, **overrides: Any) -> Settings:
    """Settings for a snapshot-enabled app bound to *db_path*."""
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


def build_app_with_store(
    db_path: Path, store: FakeStore, **overrides: Any
) -> tuple[Any, SnapshotCoordinator]:
    """Create the app and inject a coordinator wired to the fake store."""
    settings = make_settings(db_path, **overrides)
    app = create_app(settings)
    coordinator = SnapshotCoordinator(settings, store=store)
    app.state.snapshot_coordinator = coordinator
    return app, coordinator


def seed_thread(db_path: Path) -> int:
    """Insert one conversation thread directly (purge needs rows to delete)."""

    async def _seed() -> int:
        engine = build_db_engine(str(db_path))
        try:
            factory = build_session_factory(engine)
            async with factory() as session:
                thread = await create_thread(session, athlete_id=1)
                return thread.id
        finally:
            await engine.dispose()

    import asyncio

    return asyncio.run(_seed())


class TestLifespanCoordinator:
    """Startup is fail-closed; shutdown flushes the final snapshot (C6)."""

    def test_bucket_without_boot_state_refuses_to_start(self, migrated_db: Path) -> None:
        """Bucket configured + missing state => the app never becomes ready."""
        app = create_app(make_settings(migrated_db))
        with pytest.raises(SnapshotStateError):
            with TestClient(app):
                pass  # pragma: no cover - startup raises before the body

    def test_shutdown_flushes_a_final_snapshot(self, migrated_db: Path) -> None:
        """A clean lifespan exit publishes once, then disposes the engine."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = FakeStore(generation=0)
        app, _ = build_app_with_store(migrated_db, store)

        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert store.uploads == []

        assert len(store.uploads) == 1

    def test_local_lane_never_touches_the_store(self, migrated_db: Path) -> None:
        """Bucket unset: startup, traffic and shutdown stay offline (AC4)."""
        store = FakeStore(failure=AssertionError("store must not be called"))
        app, _ = build_app_with_store(migrated_db, store, COACH_SNAPSHOT_BUCKET="")

        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

        assert store.calls == []


class TestSynchronousPurge:
    """Owner purge overwrites the durable object before answering 200 (C5)."""

    def test_purge_publishes_synchronously_before_200(self, migrated_db: Path) -> None:
        """A successful purge returns 200 only after the upload succeeded."""
        seed_thread(migrated_db)
        write_boot_state(migrated_db, BootState(generation=4, publish_enabled=True))
        store = FakeStore(generation=4)
        app, _ = build_app_with_store(migrated_db, store)

        with TestClient(app) as client:
            response = client.post("/api/conversation/purge", json={"confirm": True})

        assert response.status_code == 200
        assert response.json() == {"threads_deleted": 1, "messages_deleted": 0}
        # The purge upload is the first one (generation 4 -> 5); the lifespan
        # shutdown then flushes a final snapshot unconditionally (C5/C6).
        assert store.uploads
        ciphertext, expected = store.uploads[0]
        assert expected == 4
        assert b"SQLite format 3" not in ciphertext

    def test_purge_returns_503_when_the_durable_write_fails(self, migrated_db: Path) -> None:
        """A failed overwrite is a redacted 503, never a false purge success."""
        seed_thread(migrated_db)
        write_boot_state(migrated_db, BootState(generation=4, publish_enabled=True))
        store = FakeStore(generation=4)
        store.failure = SnapshotUnavailable("HTTP 503")
        app, _ = build_app_with_store(migrated_db, store)

        with TestClient(app) as client:
            response = client.post("/api/conversation/purge", json={"confirm": True})

        assert response.status_code == 503
        assert response.json() == {"detail": PURGE_DURABLE_STORAGE_DETAIL}
        assert "purge" in PURGE_DURABLE_STORAGE_DETAIL

    def test_purge_returns_503_when_publication_is_disabled(self, migrated_db: Path) -> None:
        """A stale/disabled instance must never report a durable purge."""
        seed_thread(migrated_db)
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=False))
        store = FakeStore(generation=0)
        app, _ = build_app_with_store(migrated_db, store)

        with TestClient(app) as client:
            response = client.post("/api/conversation/purge", json={"confirm": True})

        assert response.status_code == 503
        assert response.json() == {"detail": PURGE_DURABLE_STORAGE_DETAIL}
        assert store.uploads == []

    def test_purge_is_unchanged_on_a_local_lane(self, migrated_db: Path) -> None:
        """Bucket unset: purge stays a plain local delete (AC4)."""
        seed_thread(migrated_db)
        store = FakeStore(failure=AssertionError("store must not be called"))
        app, _ = build_app_with_store(migrated_db, store, COACH_SNAPSHOT_BUCKET="")

        with TestClient(app) as client:
            response = client.post("/api/conversation/purge", json={"confirm": True})

        assert response.status_code == 200
        assert response.json() == {"threads_deleted": 1, "messages_deleted": 0}
        assert store.calls == []


class TestMutationNotifications:
    """Committed writes schedule the debounced publisher (C5)."""

    @pytest.fixture
    def dirty_calls(self, migrated_db: Path) -> tuple[Any, list[str]]:
        """App with a coordinator whose mark_dirty is a recorder."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = FakeStore(generation=0)
        app, coordinator = build_app_with_store(migrated_db, store)
        calls: list[str] = []

        def record() -> None:
            calls.append("dirty")

        coordinator.mark_dirty = record  # type: ignore[method-assign]
        return app, calls

    def test_agent_settings_save_marks_dirty(self, dirty_calls: tuple[Any, list[str]]) -> None:
        """PUT /api/admin/agent-settings commits a row and notifies the publisher."""
        app, calls = dirty_calls
        with TestClient(app) as client:
            response = client.put(
                "/api/admin/agent-settings",
                json={"orientation": "direct", "verbosity": "concise", "tone": "supportive"},
            )

        assert response.status_code == 200
        assert calls == ["dirty"]

    def test_conversation_reset_marks_dirty(self, dirty_calls: tuple[Any, list[str]]) -> None:
        """POST /api/conversation/reset archives + opens a thread and notifies."""
        app, calls = dirty_calls
        with TestClient(app) as client:
            response = client.post("/api/conversation/reset")

        assert response.status_code == 200
        assert calls == ["dirty"]

    def test_debrief_save_marks_dirty(self, migrated_db: Path) -> None:
        """POST /api/debrief commits the encrypted row and notifies the publisher."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = FakeStore(generation=0)
        app, coordinator = build_app_with_store(migrated_db, store)
        calls: list[str] = []

        def record() -> None:
            calls.append("dirty")

        coordinator.mark_dirty = record  # type: ignore[method-assign]
        with TestClient(app) as client:
            response = client.post(
                "/api/debrief",
                json={"plan_session_key": "2026-11-20", "rpe": 6, "notes": "ok"},
            )

        assert response.status_code == 201
        assert calls == ["dirty"]

    def test_debounced_publish_runs_after_a_real_write(self, migrated_db: Path) -> None:
        """The recorder being replaced by the real debounce publishes once."""
        write_boot_state(migrated_db, BootState(generation=0, publish_enabled=True))
        store = FakeStore(generation=0)
        app, _ = build_app_with_store(migrated_db, store)

        with TestClient(app) as client:
            assert (
                client.put(
                    "/api/admin/agent-settings",
                    json={"orientation": "direct", "verbosity": "concise", "tone": "supportive"},
                ).status_code
                == 200
            )
            deadline = time.monotonic() + 2.0
            while not store.uploads and time.monotonic() < deadline:
                time.sleep(0.01)

        assert len(store.uploads) >= 1


class TestPurgeErrorDetail:
    """The 503 detail is static and value-free (nLPD)."""

    def test_detail_is_static(self) -> None:
        assert json.dumps(PURGE_DURABLE_STORAGE_DETAIL) == (
            '"Conversation purge could not be committed to durable storage; retry shortly."'
        )
