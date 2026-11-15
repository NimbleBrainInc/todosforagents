"""The runtime contract: what a deployment supplies, and how one is chosen.

The core (models, tables, store, tools, UI, skill) is the same in every deployment. What
differs is a small set of implementations, a **runtime**: where identity comes from, which
bearer tokens the HTTP transport accepts, how a database session opens, what limits a scope
works under, what an event records, what else the server serves, and what wraps its HTTP app.
Configuration chooses the runtime: ``TODOS_RUNTIME`` names one, and each installed runtime
registers itself under the ``mcp_todos.runtimes`` entry-point group, so this module names no
runtime package.

The server refuses to start when ``TODOS_RUNTIME`` is unset or names no installed runtime. It
never infers one from other settings: a multi-principal deployment that silently fell back to
a single-principal runtime would drop its isolation. A runtime's own command may select it
explicitly (``todosforagents`` selects the local runtime).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.server.auth import AuthProvider
    from starlette.types import ASGIApp

__all__ = [
    "ENTRY_POINT_GROUP",
    "RUNTIME_ENV",
    "AuthError",
    "Limits",
    "Overloaded",
    "Principal",
    "Runtime",
    "RuntimeSelectionError",
    "Scope",
    "TodoEvent",
    "select_runtime",
]

RUNTIME_ENV = "TODOS_RUNTIME"
ENTRY_POINT_GROUP = "mcp_todos.runtimes"


class Principal(Protocol):
    """Who a store call acts for: the identity every row it writes is stamped with."""

    @property
    def tenant_id(self) -> str: ...
    @property
    def workspace_id(self) -> str: ...
    @property
    def user_id(self) -> str | None: ...
    @property
    def correlation_id(self) -> str | None: ...
    def identity_columns(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class Scope:
    """The verified identity of one call. Never built from a tool argument."""

    tenant_id: str
    workspace_id: str
    user_id: str | None = None
    correlation_id: str | None = None

    def identity_columns(self) -> dict[str, str]:
        """The ``tenant_id`` / ``workspace_id`` columns a row written under this scope carries."""
        return {"tenant_id": self.tenant_id, "workspace_id": self.workspace_id}


@dataclass(frozen=True)
class Limits:
    """How much a scope's tenant may hold. ``None`` in a field means no limit on that count.

    The store enforces them inside the transaction that would exceed one, counting the
    tenant's rows the caller's session can see. Where row-level security scopes a session to
    one workspace, that is the caller's workspace, so a limit holds per workspace. It counts
    every board, and every **open** to-do, one
    whose status is neither ``done`` nor ``cancelled`` and that is not archived. Completing,
    cancelling or archiving a to-do frees room; any move that makes a finished to-do open
    again counts.
    """

    max_boards: int | None = None
    max_open_todos: int | None = None


class AuthError(Exception):
    """No verified identity could be established for the call: fail closed.

    ``next_step`` is the guidance the tool result carries; a runtime sets it to say where
    identity is expected to come from.
    """

    def __init__(self, message: str, *, next_step: str | None = None) -> None:
        super().__init__(message)
        self.next_step = next_step or (
            "No verified identity could be read from the request. Reach this server through "
            "the deployment's authenticated entry point."
        )


class Overloaded(Exception):
    """The call was refused for capacity before it ran anything; a retry is safe.

    ``message`` and ``next_step`` are host-safe text, returned as the ``rate_limited`` result.
    """

    def __init__(
        self,
        message: str = "The server is at capacity right now. Nothing was run.",
        next_step: str = "Wait a few seconds, then retry the call.",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.next_step = next_step


class TodoEvent(StrEnum):
    """A domain event a runtime may record (count, log, or ignore)."""

    CREATED = "created"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    #: A tool resolved a to-do id to not-found: absent, or out of the caller's scope.
    TODO_DENIED = "todo_denied"
    #: A tool resolved a board id to not-found.
    BOARD_DENIED = "board_denied"
    #: A call was refused because it would exceed :attr:`Limits.max_boards`.
    BOARD_LIMIT_REFUSED = "board_limit_refused"
    #: A call was refused because it would exceed :attr:`Limits.max_open_todos`.
    OPEN_TODO_LIMIT_REFUSED = "open_todo_limit_refused"


class Runtime(Protocol):
    """What a deployment supplies. The tools call ``identity`` then ``session``."""

    #: The name it registers under in the ``mcp_todos.runtimes`` group.
    name: str

    def identity(self) -> Scope:
        """The calling identity. Raises :class:`AuthError` when none is verified."""
        ...

    def auth(self) -> AuthProvider | None:
        """The FastMCP auth provider that gates the HTTP transport, or ``None`` for none.

        :func:`mcp_todos.server.install` sets it on the server before the HTTP app is built,
        so every request without a token it verifies is refused with 401 and a
        ``WWW-Authenticate`` challenge before any tool runs. ``identity`` then reads the
        verified token (``fastmcp.server.dependencies.get_access_token``). A runtime that
        guards HTTP in ``wrap_http`` instead returns ``None``.
        """
        ...

    def session(self, scope: Scope) -> AbstractAsyncContextManager[AsyncConnection]:
        """One transaction for ``scope``. May raise :class:`Overloaded` on entry."""
        ...

    def limits(self, scope: Scope) -> Limits | None:
        """What ``scope`` may hold, or ``None`` when nothing is limited."""
        ...

    def record(self, event: TodoEvent, count: int = 1) -> None:
        """Record ``count`` occurrences of ``event``."""
        ...

    def attach(self, mcp: FastMCP) -> None:
        """Register what this deployment serves beyond the tools: routes and resources."""
        ...

    def lifespan(self) -> AbstractAsyncContextManager[None]:
        """Startup and shutdown work around the server's life (schema, engine disposal)."""
        ...

    def wrap_http(self, app: ASGIApp) -> ASGIApp:
        """The HTTP app as this deployment serves it."""
        ...


class RuntimeSelectionError(RuntimeError):
    """``TODOS_RUNTIME`` does not name an installed runtime; the server must not start."""


def select_runtime(
    environ: Mapping[str, str] | None = None,
    *,
    available: Iterable[EntryPoint] | None = None,
) -> Runtime:
    """The runtime ``TODOS_RUNTIME`` names, constructed. Refuses rather than guesses.

    ``available`` is the registered entry points, for a test; by default the installed ones.
    """
    env = os.environ if environ is None else environ
    name = (env.get(RUNTIME_ENV) or "").strip()
    registered = {
        ep.name: ep
        for ep in (entry_points(group=ENTRY_POINT_GROUP) if available is None else available)
    }
    installed = ", ".join(sorted(registered)) or "none"
    if not name:
        raise RuntimeSelectionError(
            f"{RUNTIME_ENV} is not set. Set it to an installed runtime (installed: {installed})."
        )
    if name not in registered:
        raise RuntimeSelectionError(
            f"{RUNTIME_ENV}={name!r} names no installed runtime (installed: {installed})."
        )
    factory = registered[name].load()
    return factory()
