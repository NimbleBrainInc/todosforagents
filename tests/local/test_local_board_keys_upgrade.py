"""The board-keys revision backfills a local database from the release before it.

A file at revision ``0001`` holding boards and to-dos upgrades at start: the default board
takes ``TODO``, the others derive keys in creation order, to-dos are numbered per board by
``created_at`` then id, and the next to-do on a board takes the number after its highest.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from mcp_todos.db import store
from mcp_todos_local import schema, sqlite
from mcp_todos_local.runtime import LOCAL_SCOPE, LocalRuntime

BOARDS = [
    # (id, name, is_default, created_at)
    ("bd_launch", "Launch", 0, "2026-01-01 00:01:00.000000"),
    ("bd_default", "To-dos", 1, "2026-01-01 00:02:00.000000"),
    ("bd_launch2", "Launch", 0, "2026-01-01 00:03:00.000000"),
]
TODOS = [
    # (id, board, created_at); td_02 and td_01 tie on created_at, so the id orders them.
    ("td_03", "bd_launch", "2026-01-01 00:10:00.000000"),
    ("td_02", "bd_launch", "2026-01-01 00:05:00.000000"),
    ("td_01", "bd_launch", "2026-01-01 00:05:00.000000"),
    ("td_04", "bd_default", "2026-01-01 00:01:00.000000"),
]


def _previous_release(path: Path) -> None:
    config = schema.alembic_config()
    engine = sqlite.configure(create_engine(f"sqlite:///{path}", poolclass=NullPool))
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "0001")
    finally:
        engine.dispose()
    with sqlite3.connect(path) as conn:
        for board_id, name, is_default, at in BOARDS:
            conn.execute(
                "INSERT INTO board (id, tenant_id, workspace_id, name, is_default, created_at, "
                "updated_at) VALUES (?, 'local', 'local', ?, ?, ?, ?)",
                (board_id, name, is_default, at, at),
            )
        for todo_id, board_id, at in TODOS:
            conn.execute(
                "INSERT INTO todo (id, tenant_id, workspace_id, board_id, title, created_at, "
                "updated_at) VALUES (?, 'local', 'local', ?, ?, ?, ?)",
                (todo_id, board_id, todo_id, at, at),
            )


async def _next_on(path: Path, board_id: str) -> str:
    rt = LocalRuntime(path)
    try:
        async with rt.session(LOCAL_SCOPE) as conn:
            todo, _ = await store.create_todo(conn, LOCAL_SCOPE, title="new", board_id=board_id)
            fetched, *_ = await store.get_todo(conn, LOCAL_SCOPE, todo_id="lau-2")
    finally:
        await rt.dispose()
    assert fetched.id == "td_02"
    return todo.key


def test_a_previous_release_upgrades_with_keys_and_numbers(tmp_path: Path) -> None:
    path = tmp_path / "todos.db"
    _previous_release(path)

    assert schema.upgrade(path) == f"{path}.0001.bak"

    with sqlite3.connect(path) as conn:
        boards = {
            r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, key, next_number FROM board")
        }
        todos = dict(conn.execute("SELECT id, number FROM todo").fetchall())
    assert boards == {"bd_launch": ("LAU", 4), "bd_default": ("TODO", 2), "bd_launch2": ("LAU2", 1)}
    assert todos == {"td_01": 1, "td_02": 2, "td_03": 3, "td_04": 1}
    assert asyncio.run(_next_on(path, "bd_launch")) == "LAU-4"
