"""Shared fixtures: every suite runs on SQLite, through the local runtime.

**The store.** Every test gets a fresh copy of a SQLite file already at the local schema's
head, behind the installed local runtime. :func:`session_for` opens that runtime's session.
The store suites act for one principal, which is the only case the SQLite store serves.

**The server.** The tools run in this process on the local runtime, installed once here. A
test's tool calls reach the same per-test database file as its store calls, because the
installed runtime is pointed at that file before the test runs.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from mcp_todos import server
from mcp_todos.runtime import Scope
from mcp_todos_local import schema as local_schema
from mcp_todos_local.runtime import LOCAL_SCOPE, LocalRuntime

_TODOS_ROOT = Path(__file__).resolve().parent.parent

#: The runtime every in-process tool call uses. Its database file changes per test.
RUNTIME = LocalRuntime(db_path=_TODOS_ROOT / ".pytest-unused.db")
server.install(RUNTIME)


@pytest.fixture(scope="session")
def sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One SQLite file at the local schema's head; each test gets a copy of it."""
    path = tmp_path_factory.mktemp("sqlite") / "template.db"
    local_schema.upgrade(path)
    return path


@pytest_asyncio.fixture(autouse=True)
async def backend(
    sqlite_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[str]:
    """A fresh database file at head, behind the installed runtime, for this test.

    It lives in a directory of its own, so it never collides with a file the test makes in
    its ``tmp_path``.
    """
    path = tmp_path_factory.mktemp("runtime") / "todos.db"
    shutil.copyfile(sqlite_template, path)
    await RUNTIME.dispose()
    RUNTIME.db_path = path
    try:
        yield "sqlite"
    finally:
        await RUNTIME.dispose()


@pytest_asyncio.fixture
async def app_engine(backend: str):
    """The engine this test's sessions use (the installed runtime's, on this test's file)."""
    yield RUNTIME.engine()
    await RUNTIME.dispose()


# --------------------------------------------------------------------------- #
# Identity + session helpers                                                   #
# --------------------------------------------------------------------------- #


def make_ctx(
    tenant_id: str,
    *,
    workspace_id: str = "ws_default",
    user_id: str | None = "user_test",
    correlation_id: str | None = "corr_test",
) -> Scope:
    """The verified identity a store call acts for."""
    return Scope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )


@pytest.fixture
def ctx_factory():
    """Expose :func:`make_ctx` as a fixture for callers that prefer injection."""
    return make_ctx


def session_for(ctx: Scope):
    """One transaction on this test's database, through the installed local runtime.

    The local runtime has one principal, so the session is the same for every ``ctx``; the
    store stamps the rows it writes with ``ctx``'s identity columns.
    """
    del ctx
    return RUNTIME.session(LOCAL_SCOPE)
