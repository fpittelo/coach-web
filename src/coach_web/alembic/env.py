"""Alembic migration environment for Coach Web (MADR-008, issue #166).

The database URL is derived from ``COACH_DB_PATH`` on every invocation — the
container entrypoint, the lane volumes and the test suite each point the
variable at their own SQLite file. Online mode only: offline SQL generation
(``alembic upgrade --sql``) is deliberately unsupported (YAGNI — migrations
run directly against the lane database at container start).

``render_as_batch=True`` is the SQLite ``ALTER TABLE`` workaround required by
MADR-008: every future schema change on SQLite is emitted as batch
(create-new-table / copy / drop / rename) operations.
"""

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from coach_web import objectives  # noqa: F401 — registers ORM tables on Base.metadata
from coach_web.db import CONTAINER_DB_PATH, Base

config = context.config

db_path = os.environ.get("COACH_DB_PATH", CONTAINER_DB_PATH)
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

target_metadata = Base.metadata


def run_migrations_online() -> None:
    """Run the migrations against the configured SQLite database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
