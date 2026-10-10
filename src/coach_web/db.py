"""Persistence engine for Coach Web (MADR-008, issue #166).

The store is SQLite in WAL mode accessed through SQLAlchemy 2.0's async
engine over the ``aiosqlite`` driver — the repo is async-first (FastAPI SSE
loop), so repository functions are ``async def`` and no call site needs a
threadpool bridge. Alembic migrations keep a plain sync ``sqlite:///`` URL
(see ``alembic/env.py``); the async driver is only used at request time.

Every raw DBAPI connection carries the MADR-008 pragmas:

- ``journal_mode=WAL`` — reads run concurrent with the single writer and
  readers never block; the ``-wal``/``-shm`` files live alongside the
  database file inside the lane volume.
- ``foreign_keys=ON`` — SQLite defaults FK enforcement off per connection;
  MADR-008 declares ``ON DELETE CASCADE`` relationships, so enforcement is
  wired here rather than hoped for.
"""

from typing import Any

from sqlalchemy import event, pool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

CONTAINER_DB_PATH = "/data/coach.db"
"""Default database file inside the container (lane volume mount at /data)."""


class Base(DeclarativeBase):
    """Declarative base for every Coach Web ORM model."""


def build_db_engine(db_path: str) -> AsyncEngine:
    """Build the async engine for the SQLite database at *db_path*.

    The engine is lazy — no filesystem access happens until a connection is
    actually opened, so constructing it per app instance is cheap.

    ``NullPool`` is deliberate (KIS, n=1): pooling adds nothing for a
    single-user SQLite lane (no auth/handshake to amortize), and it closes
    every connection deterministically when its session closes — no pooled
    aiosqlite connections can linger in an engine that is never disposed
    (e.g. apps exercised without their lifespan), which would otherwise
    surface as ``Connection.__del__`` garbage-collection noise under the
    zero-warning gate.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=pool.NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        """Apply the MADR-008 pragmas to every new raw connection."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to *engine*.

    ``expire_on_commit=False`` keeps loaded ORM rows usable after the commit
    that persists them (the repository commits inside the save call).
    """
    return async_sessionmaker(engine, expire_on_commit=False)
