"""aiosqlite worker teardown tolerance (#293).

aiosqlite runs every connection's SQL on a private worker thread and
delivers each result through ``future.get_loop().call_soon_threadsafe(...)``.
When a connection's event loop closes before the worker delivers — the #293
teardown race (an un-awaited stop sentinel from a failed connect, or a
GC-driven terminate after the loop shut down) — that call raises
``RuntimeError("Event loop is closed")`` and aiosqlite's except-handler
raises it a second time while delivering the exception, killing the worker
thread. pytest's threadexception plugin surfaces the death as
``PytestUnhandledThreadExceptionWarning`` — fatal under the zero-warning
gate (``-W error``); it fired once in CI (#292 run #332).

The conftest installs a closed-loop-tolerant worker (see
``tests/conftest.py``): a delivery to a closed loop is dropped — the future
is orphaned, nobody can await it — so the worker always drains its queue and
exits cleanly. These tests pin that contract deterministically: the hazard
is staged with the worker NOT yet started, the loop is closed first, and the
worker is started last, so the delivery-to-closed-loop ordering is
guaranteed — no timing, no flake.
"""

import asyncio
import threading
import traceback
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest


@pytest.fixture
def worker_deaths() -> Iterator[list[threading.ExceptHookArgs]]:
    """Spy on ``threading.excepthook`` and record any worker thread death.

    The spy deliberately does NOT forward to the previous hook: the death (if
    any) must be judged by this module's assertions alone, so the failure is
    a clean ``AssertionError`` naming the surviving hazard instead of a
    phase-end warning error.
    """
    deaths: list[threading.ExceptHookArgs] = []
    previous = threading.excepthook

    def spy(args: threading.ExceptHookArgs) -> None:
        deaths.append(args)

    threading.excepthook = spy
    try:
        yield deaths
    finally:
        threading.excepthook = previous


def _closed_current_loop() -> asyncio.AbstractEventLoop:
    """Create a loop, make it the thread-current loop, and close it.

    Python 3.12's ``asyncio.get_event_loop()`` returns a closed loop that is
    still set as current — the exact precondition aiosqlite's ``stop()``
    relies on when it binds its stop-future (and the precondition the #293
    race exploits when the loop closes before the worker delivers).
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.close()
    return loop


@pytest.fixture
def restored_loop_policy() -> Any:
    """Restore the thread-current event loop after a test moved it."""
    with warnings.catch_warnings():
        # Python 3.12 deprecation-noise guard (mirrors pytest-asyncio's own
        # _get_event_loop_no_warn): reading the current loop with none set
        # warns; under -W error that must not surface from a fixture.
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            previous: asyncio.AbstractEventLoop | None = asyncio.get_event_loop()
        except RuntimeError:
            previous = None
    try:
        yield
    finally:
        asyncio.set_event_loop(previous)


def _join_and_assert_survival(
    thread: threading.Thread, deaths: list[threading.ExceptHookArgs]
) -> None:
    """Join the worker and assert it exited WITHOUT an unhandled exception."""
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "aiosqlite worker thread did not exit"
    assert not deaths, (
        "aiosqlite worker thread died with an unhandled exception "
        f"({len(deaths)} death(s)); first:\n"
        + "".join(
            "".join(
                traceback.format_exception(death.exc_type, death.exc_value, death.exc_traceback)
            )
            for death in deaths[:1]
        )
    )


class TestClosedLoopDeliveryTolerance:
    """The conftest worker patch: delivery to a closed loop never kills."""

    def test_stop_sentinel_delivered_after_loop_close_is_tolerated(
        self,
        tmp_path: Path,
        worker_deaths: list[threading.ExceptHookArgs],
        restored_loop_policy: Any,
    ) -> None:
        """The #293 CI signature: a stop sentinel delivered to a closed loop.

        Reproduces the exact mechanism from CI run #332 — ``stop()`` queues
        ``(future, close_and_stop)`` with the future bound to a loop that
        closes before the worker delivers. Staged fully deterministically:
        the sentinel is queued while the worker is not yet running, the loop
        is closed, and only then is the worker started.
        """
        loop = _closed_current_loop()
        connection = aiosqlite.connect(str(tmp_path / "sentinel.db"))
        # Queue the stop sentinel exactly as aiosqlite's failed-`_connect()`
        # path does: the future is bound to `loop`, and nothing ever awaits it.
        stop_future = connection.stop()
        assert stop_future is not None, "stop() must bind a future while the loop is open"
        assert loop.is_closed(), "the loop must be closed before the worker runs"

        connection._thread.start()
        _join_and_assert_survival(connection._thread, worker_deaths)

    def test_failed_operation_delivered_after_loop_close_is_tolerated(
        self,
        tmp_path: Path,
        worker_deaths: list[threading.ExceptHookArgs],
        restored_loop_policy: Any,
    ) -> None:
        """The except-handler delivery site: an exception delivered to a
        closed loop must not kill the worker either (the second
        ``call_soon_threadsafe`` call site in the worker loop).
        """

        def _failing_operation() -> Any:
            raise RuntimeError("operation exploded")

        loop = _closed_current_loop()
        connection = aiosqlite.connect(str(tmp_path / "exception.db"))
        future: asyncio.Future[Any] = loop.create_future()
        connection._tx.put_nowait((future, _failing_operation))
        # The stop sentinel ends the worker after the failed delivery; its
        # own future is bound to the (now closing) loop as well.
        connection.stop()
        assert loop.is_closed(), "the loop must be closed before the worker runs"

        connection._thread.start()
        _join_and_assert_survival(connection._thread, worker_deaths)

    def test_live_loop_delivery_still_resolves_futures(
        self,
        tmp_path: Path,
        worker_deaths: list[threading.ExceptHookArgs],
        restored_loop_policy: Any,
    ) -> None:
        """The tolerance never masks a live-loop delivery: results and
        exceptions still reach their futures when the loop is running.
        """
        results: list[Any] = []
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            connection = aiosqlite.connect(str(tmp_path / "live.db"))

            async def _exercise() -> None:
                await connection  # start worker + connect
                await connection.execute("CREATE TABLE t (x INTEGER)")
                await connection.execute("INSERT INTO t VALUES (7)")
                await connection.commit()
                cursor = await connection.execute("SELECT x FROM t")
                results.append(await cursor.fetchone())
                await connection.close()

            loop.run_until_complete(_exercise())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

        assert results and results[0][0] == 7
        assert not worker_deaths, "a live-loop suite must never record a death"
