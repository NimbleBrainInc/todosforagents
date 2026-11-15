"""The package loads no Postgres driver and no optional SDK extra.

Importing the core (``mcp_todos``, its server, store and tables) and the local runtime loads no
``asyncpg``, no SDK ``[db]`` module and no SDK ``[observability]`` module: none is a dependency
of this package. Each check runs in a fresh interpreter, so this suite's own imports cannot hide
a load.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN = ("asyncpg", "nimblebrain_server_sdk.db", "nimblebrain_server_sdk.observability")

PUBLIC_IMPORTS = (
    "mcp_todos",
    "mcp_todos.server",
    "mcp_todos.runtime",
    "mcp_todos.db.store",
    "mcp_todos.db.tables",
    "mcp_todos.__main__",
    "mcp_todos_local",
    "mcp_todos_local.runtime",
    "mcp_todos_local.schema",
    "mcp_todos_local.__main__",
)


def _loaded_after(statement: str) -> list[str]:
    probe = f"import json, sys\n{statement}\nprint(json.dumps(sorted(sys.modules)))"
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(done.stdout)


def _forbidden(modules: list[str]) -> list[str]:
    return [m for m in modules if any(m == f or m.startswith(f"{f}.") for f in FORBIDDEN)]


@pytest.mark.parametrize("module", PUBLIC_IMPORTS)
def test_a_public_import_loads_nothing_private(module: str) -> None:
    assert _forbidden(_loaded_after(f"import {module}")) == []


def test_the_local_runtime_serving_loads_nothing_private(tmp_path: Path) -> None:
    # Selecting the local runtime and building its HTTP app, as an install does. Building it
    # makes the install's token beside the database, so the database is a throwaway one.
    db = str(tmp_path / "todos.db")
    statement = (
        f"import os; os.environ['TODOS_RUNTIME'] = 'local'; os.environ['TODOS_LOCAL_DB'] = {db!r}"
        "\nfrom mcp_todos import server; server.app"
    )
    assert _forbidden(_loaded_after(statement)) == []


def test_the_probe_sees_what_a_statement_loads() -> None:
    # The checks above are only meaningful if the probe sees a module load, and the filter
    # matches a forbidden module and its submodules.
    assert "mcp_todos.db.store" in _loaded_after("import mcp_todos.server")
    assert _forbidden(["asyncpg", "asyncpg.connection", "nimblebrain_server_sdk.db.engine"]) == [
        "asyncpg",
        "asyncpg.connection",
        "nimblebrain_server_sdk.db.engine",
    ]
    assert _forbidden(["asyncpgx", "nimblebrain_server_sdk.dbx"]) == []
