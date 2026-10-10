"""Persistence engine contract: SQLite WAL pragmas and session factory (#166).

MADR-008 fixes the store as SQLite in WAL mode (reads concurrent with the
single writer, readers never block). The engine builder wires the pragmas on
every raw DBAPI connection — ``journal_mode=WAL`` (WAL files live alongside
the database file in the lane volume) and ``foreign_keys=ON`` (MADR-008 FK
discipline) — and the session factory binds :mod:`sqlalchemy.ext.asyncio`
sessions over that engine.
"""

from pathlib import Path

from sqlalchemy import text

from coach_web.db import build_db_engine, build_session_factory


class TestBuildDbEngine:
    """The async engine wires the MADR-008 SQLite pragmas on connect."""

    async def test_journal_mode_is_wal(self, db_path: Path) -> None:
        """Every connection sees journal_mode=wal (WAL files sit alongside)."""
        engine = build_db_engine(str(db_path))
        try:
            async with engine.connect() as conn:
                mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        finally:
            await engine.dispose()

        assert mode == "wal"

    async def test_foreign_keys_are_enforced(self, db_path: Path) -> None:
        """Every connection enforces foreign keys (MADR-008 FK discipline)."""
        engine = build_db_engine(str(db_path))
        try:
            async with engine.connect() as conn:
                enabled = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        finally:
            await engine.dispose()

        assert enabled == 1

    async def test_connecting_materializes_the_database_file(self, db_path: Path) -> None:
        """The first connection creates the database file at the configured path."""
        engine = build_db_engine(str(db_path))
        try:
            async with engine.connect():
                pass
        finally:
            await engine.dispose()

        assert db_path.exists()


class TestBuildSessionFactory:
    """The session factory yields working async sessions over the engine."""

    async def test_session_executes_a_trivial_query(self, db_path: Path) -> None:
        """Sessions execute against the bound engine."""
        engine = build_db_engine(str(db_path))
        factory = build_session_factory(engine)
        try:
            async with factory() as session:
                result = await session.execute(text("SELECT 1"))
                assert result.scalar() == 1
        finally:
            await engine.dispose()

    async def test_sessions_share_the_engine_pragmas(self, db_path: Path) -> None:
        """A session's connection carries the WAL pragma too."""
        engine = build_db_engine(str(db_path))
        factory = build_session_factory(engine)
        try:
            async with factory() as session:
                mode = (await session.execute(text("PRAGMA journal_mode"))).scalar()
                assert mode == "wal"
        finally:
            await engine.dispose()
