"""The local runtime: one principal, one SQLite file, no network beyond loopback.

| Concern    | Here                                                                 |
|------------|----------------------------------------------------------------------|
| identity   | always ``Scope("local", "local")``; request headers are ignored      |
| auth       | none from FastMCP; ``wrap_http`` guards HTTP itself                  |
| session    | aiosqlite, WAL, ``BEGIN IMMEDIATE``, busy timeout, foreign keys on   |
| limits     | none                                                                 |
| record     | nothing                                                              |
| attach     | nothing beyond the core's tools, skill and UI                        |
| lifespan   | the schema upgraded (:mod:`mcp_todos_local.schema`) before serving   |
| wrap_http  | loopback-only clients, ``Host`` and ``Origin`` (:mod:`.http`), then  |
|            | the per-install bearer token (:mod:`.auth`)                          |

The store is safe here only because there is exactly one principal: no query carries a
tenant predicate, and SQLite has no row-level security. A runtime with more than one
principal needs a database that enforces isolation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from starlette.types import ASGIApp

from mcp_todos.runtime import Limits, Overloaded, Scope, TodoEvent
from mcp_todos_local import auth, schema, sqlite
from mcp_todos_local.config import database_path
from mcp_todos_local.http import LoopbackGuard

logger = logging.getLogger(__name__)

#: The installation's one principal.
LOCAL_SCOPE = Scope(tenant_id="local", workspace_id="local")


class LocalRuntime:
    """To-dos on the user's own machine, stored in the SQLite file at ``db_path``."""

    name = "local"

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path if db_path is not None else database_path()
        self._engine: AsyncEngine | None = None

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
            sqlite.configure(engine.sync_engine)
            self._engine = engine
        return self._engine

    def identity(self) -> Scope:
        return LOCAL_SCOPE

    def auth(self) -> AuthProvider | None:
        # Local HTTP has no authorization server to point a client at: the loopback checks
        # and the per-install bearer token in wrap_http guard it.
        return None

    def limits(self, scope: Scope) -> Limits | None:
        return None

    @asynccontextmanager
    async def session(self, scope: Scope) -> AsyncGenerator[AsyncConnection]:
        try:
            async with self.engine().begin() as conn:
                yield conn
        except OperationalError as exc:
            if sqlite.is_busy(exc):
                raise Overloaded(
                    "The local database is busy with another process's write. Nothing was run.",
                    "Wait a moment, then retry the call.",
                ) from exc
            raise

    def record(self, event: TodoEvent, count: int = 1) -> None:
        return None

    def attach(self, mcp: FastMCP) -> None:
        return None

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None]:
        await asyncio.to_thread(schema.upgrade, self.db_path)
        try:
            yield
        finally:
            await self.dispose()

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def wrap_http(self, app: ASGIApp) -> ASGIApp:
        # The loopback checks run first, so a foreign page or host is refused (403) before
        # its token is considered; the token then keeps out other local processes (401).
        path = auth.token_path(self.db_path)
        guarded = auth.BearerGuard(app, auth.load_or_create(path))
        logger.info("local HTTP requires the bearer token stored in %s", path)
        return LoopbackGuard(guarded)
