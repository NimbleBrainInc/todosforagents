"""The local schema: its own Alembic history (no row-level security), run at start.

:func:`upgrade` brings a database file to the head of the history:

- A file at head is left as it is.
- A file at an older revision is copied to ``<file>.<revision>.bak`` first, then upgraded.
- A file at a revision this code does not know was written by a newer version, and is
  refused rather than opened.
- A file that holds tables but no revision is not a To-dos database, and is refused.

The check, the copy and the upgrade run in one ``BEGIN IMMEDIATE`` transaction, so processes
starting together upgrade one at a time and each later one finds the file at head.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import NullPool

from mcp_todos_local import sqlite

SCRIPT_LOCATION = str(files("mcp_todos_local") / "migrations")


class SchemaError(RuntimeError):
    """The database file cannot be opened by this version."""


def alembic_config(script_location: str = SCRIPT_LOCATION) -> Config:
    config = Config()
    config.set_main_option("script_location", script_location)
    return config


def upgrade(path: Path, *, script_location: str = SCRIPT_LOCATION) -> str | None:
    """Bring the database at ``path`` to head. Returns the backup's path, if one was made."""
    path.parent.mkdir(parents=True, exist_ok=True)
    config = alembic_config(script_location)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    engine = sqlite.configure(create_engine(f"sqlite:///{path}", poolclass=NullPool))
    backup: str | None = None
    try:
        with engine.begin() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
            if current == head:
                return None
            if current is None:
                if inspect(conn).get_table_names():
                    raise SchemaError(f"{path} holds tables but no To-dos schema revision")
            else:
                try:
                    script.get_revision(current)
                except Exception as exc:
                    raise SchemaError(
                        f"{path} is at schema revision {current!r}, which this version does "
                        "not know: it was written by a newer version. Upgrade To-dos to open it."
                    ) from exc
                backup = f"{path}.{current}.bak"
                _copy(path, Path(backup))
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    return backup


def head_revision(script_location: str = SCRIPT_LOCATION) -> str | None:
    return ScriptDirectory.from_config(alembic_config(script_location)).get_current_head()


def _copy(source: Path, target: Path) -> None:
    """A consistent copy through SQLite's backup API, WAL contents included."""
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
