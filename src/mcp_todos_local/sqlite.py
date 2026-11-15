"""SQLite connections the way every local process opens them.

- **WAL**, so readers never block the writer and several processes can share the file.
- **``BEGIN IMMEDIATE``** for every transaction, so a transaction takes the write lock when it
  starts: two processes appending to one lane serialize, and each reads the other's
  committed ``max(sort_order)``.
- **A busy timeout**, so a process waits for the lock rather than failing at once. Switching a
  new file to WAL is the one step SQLite does not cover with it (:func:`_use_wal`).
- **Foreign keys on**, which SQLite leaves off by default.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.exc import OperationalError

BUSY_TIMEOUT_MS = 15_000
#: The first pause between attempts to switch a file to WAL; it doubles up to 50 ms.
WAL_RETRY_PAUSE_S = 0.002


def configure(engine: Engine) -> Engine:
    """Install the connection settings and ``BEGIN IMMEDIATE`` on ``engine`` (sync, or an
    async engine's ``sync_engine``)."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # The driver's own implicit BEGIN would be DEFERRED; the "begin" hook issues ours.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            _use_wal(cursor)
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def _use_wal(cursor: Any) -> None:
    """Switch the file to WAL, waiting for another process up to the busy timeout.

    On a file still in rollback-journal mode, the switch reads the header under a shared lock,
    then asks for the write lock while holding it. When another connection holds the write lock,
    SQLite refuses that upgrade with ``SQLITE_BUSY`` at once, without the busy handler: the
    holder may be waiting for this connection's shared lock to clear, so waiting here could
    deadlock. Processes opening a new file together meet it. The refused statement releases its
    shared lock, the other process completes its switch, and the retry finds the file in WAL,
    where the pragma changes nothing.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    pause = WAL_RETRY_PAUSE_S
    while True:
        try:
            cursor.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy_message(exc) or time.monotonic() >= deadline:
                raise
        time.sleep(pause)
        pause = min(pause * 2, 0.05)


def is_busy(exc: OperationalError) -> bool:
    """The database stayed locked by another process past the busy timeout."""
    return _is_busy_message(exc.orig)


def _is_busy_message(exc: BaseException | None) -> bool:
    return "database is locked" in str(exc) or "database is busy" in str(exc)
