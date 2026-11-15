"""To-dos database layer.

Re-exports the SQLAlchemy ``metadata`` and table objects so callers (store,
migrations, tests) can ``from mcp_todos.db import metadata, todo`` without
reaching into ``tables`` directly.
"""

from __future__ import annotations

from mcp_todos.db.tables import (
    board,
    metadata,
    todo,
    todo_activity,
    uq_board_default,
    uq_board_key,
    uq_todo_idem,
    uq_todo_number,
)

__all__ = [
    "metadata",
    "board",
    "todo",
    "todo_activity",
    "uq_board_default",
    "uq_board_key",
    "uq_todo_idem",
    "uq_todo_number",
]
