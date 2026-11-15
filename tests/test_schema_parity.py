"""Schema parity: the local Alembic history, at head, is exactly the tables' metadata.

The core's tables (``mcp_todos.db.tables``) are the one shape; the local SQLite history must
produce it. A table change in the core needs a migration in that history. After upgrading to
head, Alembic autogenerate against ``metadata`` must report nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, create_engine

from mcp_todos.db.tables import metadata
from mcp_todos_local import schema as local_schema


def _diff(conn: Connection) -> list[Any]:
    return compare_metadata(MigrationContext.configure(conn), metadata)


def test_local_history_at_head_matches_metadata(tmp_path: Path) -> None:
    path = tmp_path / "todos.db"
    local_schema.upgrade(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            diff = _diff(conn)
    finally:
        engine.dispose()
    assert diff == []
