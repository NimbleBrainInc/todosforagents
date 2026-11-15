"""Local schema upgrades.

- A database from the previous release upgrades with its data intact, after a copy is made.
- A database written by a newer version is refused, and left as it was.
- A file that is not a To-dos database is refused.
- A server started on a refused file exits non-zero before serving.

"The previous release" is a file at this history's head holding data, and the upgrade runs
against this history plus one more revision, the shape every later release takes.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from local_harness import REPO, local_env

from mcp_todos.db import store
from mcp_todos_local import schema
from mcp_todos_local.runtime import LOCAL_SCOPE, LocalRuntime

#: The extra revision's id: one no real revision takes.
LATER = "later"

NEXT_REVISION = '''
"""a later release: one more index"""
from alembic import op

revision = "{later}"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_todo_title_probe", "todo", ["title"])


def downgrade() -> None:
    op.drop_index("ix_todo_title_probe", table_name="todo")
'''


async def _seed(path: Path) -> list[str]:
    rt = LocalRuntime(path)
    async with rt.session(LOCAL_SCOPE) as conn:
        planned, _ = await store.plan_todos(
            conn, LOCAL_SCOPE, todos=[{"title": "Keep me"}, {"title": "And me", "labels": ["x"]}]
        )
    await rt.dispose()
    return [t.id for t in planned]


async def _titles(path: Path) -> list[str]:
    rt = LocalRuntime(path)
    async with rt.session(LOCAL_SCOPE) as conn:
        todos, _ = await store.list_todos(conn, LOCAL_SCOPE, label="x")
        everything, _ = await store.list_todos(conn, LOCAL_SCOPE)
    await rt.dispose()
    assert [t.title for t in todos] == ["And me"]
    return sorted(t.title for t in everything)


def _revision(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


@pytest.fixture
def later_history(tmp_path: Path) -> str:
    """This history plus one more revision, as the next release would ship it."""
    location = tmp_path / "history"
    shutil.copytree(schema.SCRIPT_LOCATION, location)
    head = schema.head_revision()
    (location / "versions" / "9999_later.py").write_text(
        NEXT_REVISION.format(head=head, later=LATER)
    )
    return str(location)


def test_a_new_file_is_created_at_head(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "todos.db"
    assert schema.upgrade(path) is None
    assert _revision(path) == schema.head_revision()
    assert schema.upgrade(path) is None  # at head: left as it is, no copy


def test_a_previous_release_upgrades_with_its_data_intact(
    tmp_path: Path, later_history: str
) -> None:
    path = tmp_path / "todos.db"
    previous = schema.head_revision()
    schema.upgrade(path)
    asyncio.run(_seed(path))

    backup = schema.upgrade(path, script_location=later_history)

    assert _revision(path) == LATER
    assert asyncio.run(_titles(path)) == ["And me", "Keep me"]
    assert backup is not None and backup == f"{path}.{previous}.bak"
    assert _revision(Path(backup)) == previous
    assert asyncio.run(_titles(Path(backup))) == ["And me", "Keep me"]


def test_a_newer_database_is_refused_and_left_as_it_was(tmp_path: Path, later_history: str) -> None:
    path = tmp_path / "todos.db"
    schema.upgrade(path, script_location=later_history)  # written by the next release
    asyncio.run(_seed(path))
    before = path.read_bytes()

    with pytest.raises(schema.SchemaError, match="newer version"):
        schema.upgrade(path)

    assert _revision(path) == LATER
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak"))


def test_a_file_that_is_not_a_todos_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    with pytest.raises(schema.SchemaError, match="no To-dos schema revision"):
        schema.upgrade(path)


def test_the_server_refuses_to_start_on_a_newer_database(
    tmp_path: Path, later_history: str
) -> None:
    path = tmp_path / "todos.db"
    schema.upgrade(path, script_location=later_history)
    done = subprocess.run(
        [sys.executable, "-m", "mcp_todos"],
        env=local_env(path),
        cwd=str(REPO),
        stdin=subprocess.PIPE,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "newer version" in done.stderr
    assert done.stdout == ""
