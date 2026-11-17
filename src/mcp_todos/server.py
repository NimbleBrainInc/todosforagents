"""To-dos MCP Server — the tools, the skill and the UI, on whichever runtime is installed.

The **only public door** to the To-dos working list (Board · To-do · TodoActivity).
Every tool:

  1. reads the call's verified :class:`~mcp_todos.runtime.Scope` from the runtime
     (``runtime().identity()``) — identity is NEVER read from a tool argument or
     request body;
  2. opens one transaction through the runtime (``runtime().session(scope)``) and calls
     the matching store function (the store stamps tenant/workspace from the scope);
  3. returns the documented dict INCLUDING a non-empty ``next_step`` string, which
     tells the agent what to do with the result.

A failure returns a tool result with ``isError: true`` and one body for every kind:
``{"error": {"code", "message", "retryable", ...}, "next_step"}``. Out-of-scope reads/writes
are ``not_found`` (404 semantics, never an existence oracle); missing identity, bad arguments
and unexpected faults have their own codes, never a 500/stack trace to the agent.

The runtime (:mod:`mcp_todos.runtime`) is what differs between deployments: where identity
comes from, how a session opens, what an event records, and what the server serves beyond
these tools. ``TODOS_RUNTIME`` selects it, and :func:`install` binds it once per process.
Importing this module stays free of the environment and the database, so tooling can
introspect the tool surface.

Entry points: ``mcp_todos.server:app`` is the ASGI app (built on first access, for the
runtime ``TODOS_RUNTIME`` names), and ``python -m mcp_todos`` serves stdio.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from typing import Any

import mcp.types as mcp_types
from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from nimblebrain_server_sdk import (
    ADDS,
    CHANGES,
    PREVIEW_CHARS,
    READS,
    SkillDefinition,
    add_skills,
    announce_resource_list_changed,
    listing_row,
)
from nimblebrain_server_sdk.errors import rate_limited_tool_error

from mcp_todos import prompts
from mcp_todos.db import store
from mcp_todos.db.store import (
    BoardNotFoundError,
    LimitExceededError,
    TodoNotFoundError,
)
from mcp_todos.models import Board, PlanItem, Status, Todo, dump
from mcp_todos.runtime import AuthError, Overloaded, Runtime, Scope, TodoEvent, select_runtime
from mcp_todos.ui import load_ui

SERVICE = "todos"
#: The distribution this package ships in, whose version the server reports.
DISTRIBUTION = "todosforagents"

logger = logging.getLogger("mcp_todos")


# --------------------------------------------------------------------------- #
# The runtime: bound once per process.                                         #
# --------------------------------------------------------------------------- #

_runtime: Runtime | None = None


def install(rt: Runtime) -> Runtime:
    """Bind ``rt`` as this process's runtime, set its auth provider, and let it attach.

    A process serves one runtime: binding a second, different one is refused rather than
    letting tools and routes disagree about where identity comes from.

    The runtime is chosen after this module builds ``mcp``, so its auth provider cannot go to
    the ``FastMCP`` constructor. It does not need to: ``FastMCP.auth`` is read when
    ``http_app()`` builds the Starlette app, which happens after this (``app`` installs the
    runtime first). Setting it here puts the provider's middleware, its 401 challenge and its
    metadata routes in front of the MCP endpoint, the same as passing ``auth=`` would.
    """
    global _runtime
    if _runtime is not None:
        if _runtime is rt:
            return rt
        raise RuntimeError(f"the {_runtime.name!r} runtime is already installed")
    mcp.auth = rt.auth()
    rt.attach(mcp)
    _runtime = rt
    return rt


def runtime() -> Runtime:
    """The installed runtime. Raises if none is, so no call runs without one."""
    if _runtime is None:
        raise RuntimeError("no To-dos runtime is installed; set TODOS_RUNTIME")
    return _runtime


def installed() -> Runtime:
    """The installed runtime, installing the one ``TODOS_RUNTIME`` names if none is yet."""
    return _runtime if _runtime is not None else install(select_runtime())


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncGenerator[None]:
    """The runtime's startup and shutdown work around the server's life."""
    async with runtime().lifespan():
        logger.info("todos_startup", extra={"service": SERVICE})
        try:
            yield
        finally:
            logger.info("todos_shutdown", extra={"service": SERVICE})


def _package_version() -> str:
    """The installed ``todosforagents`` version, or ``dev`` when it is not installed."""
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return "dev"


# The product's mark, sent inline in serverInfo so a host shows it with no network fetch.
_ICON = mcp_types.Icon(
    src="data:image/png;base64,"
    + base64.b64encode(files("mcp_todos").joinpath("icon.png").read_bytes()).decode("ascii"),
    mime_type="image/png",
    sizes=["128x128"],
)

mcp: FastMCP = FastMCP(
    "To-dos",
    # SERVICE_VERSION when a deployment sets it, else the installed package's version.
    version=os.environ.get("SERVICE_VERSION") or _package_version(),
    icons=[_ICON],
    lifespan=_lifespan,
    # Standalone: a host that never reads the skill still gets the rules that pick a tool.
    instructions=(
        "A to-do's status moves only through the transition verbs; update_todo cannot change "
        "it. start_todo when work begins, complete_todo when it is finished, cancel_todo when "
        "it will not be done, block_todo with a reason when it is waiting on something, "
        "reopen_todo to put it back to open. archive_todo only hides a to-do and leaves its "
        "status as it is. A to-do's key (MAG-3) and a board's key (MAG) work anywhere an id "
        "does. Hosts that load skills can read skill://todos/SKILL.md for more."
    ),
)

# Agent guidance, bundled inside the package so it ships with the wheel. Published through
# the MCP Skills extension, which lists it in skills/list and serves it at
# skill://todos/SKILL.md; the server instructions above point at it.
SKILL_CONTENT = files("mcp_todos").joinpath("SKILL.md").read_text(encoding="utf-8")
add_skills(mcp, [SkillDefinition("todos", {"SKILL.md": SKILL_CONTENT})])


# --------------------------------------------------------------------------- #
# Annotations. Every tool acts on this server's own database and nothing else, #
# so each is closed-world (the spec's default is open). Idempotent means a     #
# second identical call leaves the to-do as the first did; the activity row    #
# and ``updated_at`` it also writes are bookkeeping, not part of that.         #
# --------------------------------------------------------------------------- #

_CLOSED: dict[str, Any] = {"open_world_hint": False}

#: A read: changes nothing, so any number of calls equals one.
READ = READS.model_copy(update={"idempotent_hint": True, **_CLOSED})
#: A create: adds a row each call (a create_todo/plan_todos key converges, but without one
#: a retry adds again), so not idempotent.
ADD = ADDS.model_copy(update={"idempotent_hint": False, **_CLOSED})
#: A field edit or reorder: overwrites values the call does not keep, and setting the same
#: values twice equals once.
EDIT = CHANGES.model_copy(update={"idempotent_hint": True, **_CLOSED})
#: A status move another verb undoes, whose result does not depend on how often it ran.
MOVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, **_CLOSED
)
#: A status move another verb undoes that stamps ``completed_at`` with the time of the call,
#: so a second call changes the to-do again.
MOVE_STAMPED = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, **_CLOSED
)
#: Archive: nothing un-archives a to-do, and each call stamps ``archived_at`` anew.
HIDE = CHANGES.model_copy(update={"idempotent_hint": False, **_CLOSED})


# --------------------------------------------------------------------------- #
# ChatGPT's keys. ChatGPT reads two OpenAI-specific ``_meta`` keys beside the  #
# MCP Apps ones; the source of truth is the OpenAI Apps SDK reference,         #
# https://developers.openai.com/plugins/reference. They are plain metadata,    #
# the same in every runtime, and other hosts ignore them.                      #
# --------------------------------------------------------------------------- #

#: On every tool. ``openai/widgetAccessible`` defaults to ``false``, and ChatGPT reads it to
#: decide whether a widget may call the tool. The board calls the reads, the transition verbs
#: and ``reorder_todos`` from its frame, and no tool is for the model alone, so every tool
#: carries ``true``.
CHATGPT_TOOL_META: dict[str, Any] = {"openai/widgetAccessible": True}

#: The product's origin, which ChatGPT renders the board under. ``openai/widgetDomain`` is
#: ChatGPT's alias for ``_meta.ui.domain``. The alias carries it rather than ``ui.domain``
#: because other MCP Apps hosts validate ``ui.domain`` against domains of their own. It names
#: the product, not a deployment, so it is a constant.
CHATGPT_WIDGET_DOMAIN = "https://todosforagents.com"

#: On the board UI resource and its build template, in the listing and in what a read returns.
CHATGPT_UI_META: dict[str, Any] = {"openai/widgetDomain": CHATGPT_WIDGET_DOMAIN}


# --------------------------------------------------------------------------- #
# Error mapping: never let a store/auth/validation error become a 500/stack     #
# trace on the wire. Out-of-scope → not_found (404 semantics, no existence       #
# oracle); auth/validation → structured tool error.                             #
# --------------------------------------------------------------------------- #


def _error_result(body: dict[str, Any]) -> Any:
    """``body`` as a tool result flagged ``isError``, its JSON as the text a model reads.

    Typed ``Any`` because the tools declare ``-> dict`` (their output schema) and FastMCP
    passes a returned ``ToolResult`` through as it is.
    """
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(body))],
        structured_content=body,
        is_error=True,
    )


def _error(
    code: str, message: str, next_step: str, *, retryable: bool = False, **context: Any
) -> Any:
    """The one error shape: ``{"error": {code, message, retryable, ...context}, next_step}``.

    The envelope's keys are the SDK error contract's (``code``, ``message``, ``retryable``),
    so a ``rate_limited`` refusal and every other failure read the same way.
    """
    return _error_result(
        {
            "error": {"code": code, "message": message, "retryable": retryable, **context},
            "next_step": next_step,
        }
    )


def _not_found(entity: str, entity_id: str) -> Any:
    """A not-found tool error (404 semantics). Records a denial event.

    Out-of-scope ids and genuinely-absent ids are indistinguishable by contract
    (RLS returns nothing either way), so we never reveal which it was — there is no
    existence oracle. The recorded event is the only observable signal.
    """
    runtime().record(TodoEvent.BOARD_DENIED if entity == "board" else TodoEvent.TODO_DENIED)
    return _error(
        "not_found",
        f"No {entity} with id {entity_id!r} is visible in this workspace.",
        f"Verify the {entity} id, or use list_{entity}s to locate the right record.",
        entity=entity,
        id=entity_id,
    )


def _row(todo_obj: Todo) -> dict[str, Any]:
    """A to-do as a row of a page (a listing, a search, a plan's echo, a goal's steps).

    ``details`` is markdown up to 64 KiB and a page runs to 200 rows, so a row carries
    ``details_preview`` (``PREVIEW_CHARS`` characters) in place
    of ``details``. The row's ``id`` is what ``get_todo`` reads the whole to-do by; To-dos
    serves no per-record resource, so a row has no ``uri``.
    """
    return listing_row(dump(todo_obj), preview={"details": PREVIEW_CHARS})


_READ_HINT = (
    "Each row carries a details_preview, not the details: read a whole to-do with "
    "get_todo(todo_id=...)."
)


def _auth_error(exc: AuthError) -> Any:
    """The ``unauthenticated`` tool error (identity could not be established)."""
    return _error("unauthenticated", str(exc), exc.next_step)


def _invalid(message: str) -> Any:
    """The ``invalid_request`` tool error (a bad argument, never a 500)."""
    return _error("invalid_request", message, "Correct the argument and retry.")


#: How many boards a next_step names before it stops listing them.
_BOARDS_NAMED = 5


def _named(boards: list[Board]) -> str:
    """Boards as a person reads them: ``"Launch plan" (LP)``, comma-separated."""
    return ", ".join(f'"{b.name}" ({b.key})' for b in boards[:_BOARDS_NAMED])


def _use_the_boards(boards: list[Board]) -> str:
    """Where to put to-dos when no other board can be made: on the board(s) there are,
    renamed if the name does not fit, with labels to separate areas."""
    if len(boards) == 1:
        only = boards[0]
        return (
            f'This account\'s board is "{only.name}" ({only.key}). Put the to-dos there '
            f'(board_id="{only.key}"); if its name does not fit, rename it with '
            f'update_board(board_id="{only.key}", name=...), and separate areas with labels.'
        )
    if boards:
        return (
            f"This account's boards are {_named(boards)}. Put the to-dos on one of them by its "
            "key; rename one with update_board if its name does not fit, and separate areas "
            "with labels."
        )
    return (
        "Put the to-dos on a board that already exists (list_boards shows them); rename it with "
        "update_board if its name does not fit, and separate areas with labels."
    )


def _onboard(*, has_board: bool) -> str:
    """The next_step for a workspace with no to-dos: start the person's list with them."""
    where = "" if has_board else "; a board is made for them"
    return (
        "No to-dos yet. Ask the person what they are working on, then propose 3 to 5 concrete "
        f"to-dos and create them with plan_todos{where}."
    )


async def _boards_for_refusal(sctx: Scope) -> list[Board]:
    """The caller's boards, read after a ``max_boards`` refusal so its next_step can name them.

    The refused write's transaction has rolled back, so this opens a fresh session. Best-effort:
    a failed read leaves the next_step general rather than masking the refusal.
    """
    try:
        async with runtime().session(sctx) as conn:
            return await store.list_boards(conn, sctx)
    except Exception:  # noqa: BLE001 - the refusal is what the caller needs; never mask it
        logger.warning("todos_refusal_boards_failed")
        return []


def _limit_refused(exc: LimitExceededError, boards: list[Board]) -> Any:
    """The ``invalid_request`` tool error for a write that would exceed the runtime's limits.

    ``limit`` names the :class:`~mcp_todos.runtime.Limits` field that refused it, so a client
    can tell this refusal from a bad argument without a new error code. The limits are the
    deployment's, which the person chose, so each next_step names what to do inside it:
    ``boards`` (for ``max_boards``) are the boards the to-dos can go on instead.
    """
    if exc.limit == "max_boards":
        runtime().record(TodoEvent.BOARD_LIMIT_REFUSED)
        next_step = _use_the_boards(boards)
    elif exc.limit == "max_total_todos":
        runtime().record(TodoEvent.TOTAL_TODO_LIMIT_REFUSED)
        next_step = (
            "This account has used its lifetime allowance of to-dos; finishing or archiving "
            "to-dos does not free room. Every to-do it holds stays readable with list_todos "
            "and get_todo."
        )
    elif exc.limit == "max_details_bytes":
        runtime().record(TodoEvent.DETAILS_LIMIT_REFUSED)
        next_step = (
            "Shorten the details to fit the limit, then retry; keep long material elsewhere and "
            "link to it."
        )
    else:
        runtime().record(TodoEvent.OPEN_TODO_LIMIT_REFUSED)
        next_step = (
            "Complete, cancel or archive to-dos that are finished or no longer needed to free "
            "room, then retry."
        )
    return _error("invalid_request", str(exc), next_step, limit=exc.limit)


def _internal_error(exc: Exception, *, where: str) -> Any:
    """A generic internal-error tool result — the final safety net for the unexpected.

    The specific handlers above route every KNOWN failure (not-found, bad argument,
    auth) to its own structured shape. This catches the UNKNOWN —
    e.g. a DB ``OperationalError`` / ``DBAPIError`` when Postgres blips — so it
    surfaces as a structured, retryable tool result instead of an opaque MCP 500
    with a stack trace on the wire.

    The full exception (type + message + traceback) is logged server-side with
    the ``where`` (tool) context for the operator; the wire body is a FIXED,
    generic message that never carries the exception text, type, or stack. We do
    NOT attempt an audit write here: the database may be exactly what is down, so
    a write would only raise again.

    A call refused for capacity (:class:`~mcp_todos.runtime.Overloaded`: the runtime had no
    room for it) is not a fault: it ran nothing, so it returns the ``rate_limited`` error
    instead. The SDK's error contract classifies and records it; its envelope goes out in
    the one shape, with the refusal's ``next_step``.
    """
    if isinstance(exc, Overloaded):
        refused = rate_limited_tool_error(exc)
        return _error_result(
            {"error": refused.structured_content["error"], "next_step": exc.next_step}
        )
    logger.error("todos_internal_error", exc_info=True, extra={"where": where})
    return _error(
        "internal_error",
        "An internal error occurred. Please retry.",
        "Retry shortly; if it persists the service may be degraded.",
        retryable=True,
    )


async def _audit_attempt(
    sctx: Scope,
    *,
    entity: str,
    action: str,
    outcome: str,
    todo_id: str | None = None,
) -> None:
    """Record an attempted-but-failed access as a ``todo_activity`` row.

    The failing op's own transaction rolled back (a NotFound raised before its
    success-path audit, or a validation error aborted it), so the attempt would
    otherwise leave NO trace — yet an out-of-scope access attempt is exactly what
    the audit table exists to capture. We open a FRESH session (the
    caller's own tenant/workspace, so the RLS ``WITH CHECK`` permits the insert)
    and write one ``outcome={denied,error}`` row.

    Best-effort by contract: an audit-write failure must NEVER mask the original
    error the caller is about to return, so we swallow it with a ``logger.warning``.
    """
    try:
        async with runtime().session(sctx) as conn:
            await store.write_audit(
                conn,
                sctx,
                todo_id=todo_id or "",
                action=action,
                outcome=outcome,
            )
    except Exception:  # noqa: BLE001 - audit is best-effort; never mask the real error
        logger.warning(
            "todos_audit_attempt_failed",
            extra={"entity": entity, "action": action, "outcome": outcome},
        )


async def _handle_tool_error(sctx: Scope, *, entity: str, action: str, exc: Exception) -> Any:
    """Record the failed attempt, then return the matching structured tool result.

    Centralizes the except-branch handling shared by every tool: a NotFound is a
    ``denied`` attempt (out-of-scope/absent id) that returns the ``not_found``
    shape; a limit refusal is a ``denied`` attempt that returns the ``invalid_request``
    shape with a ``limit`` field; a ValueError/TypeError is an ``error`` attempt (bad
    argument) that returns the ``invalid_request`` shape. ``AuthError`` is handled by the
    caller BEFORE a scope exists (no tenant/workspace to scope an audit row to), so it never
    reaches here.
    """
    if isinstance(exc, LimitExceededError):
        await _audit_attempt(sctx, entity=entity, action=action, outcome="denied")
        boards = await _boards_for_refusal(sctx) if exc.limit == "max_boards" else []
        return _limit_refused(exc, boards)
    if isinstance(exc, TodoNotFoundError):
        await _audit_attempt(sctx, entity="todo", action=action, outcome="denied", todo_id=str(exc))
        return _not_found("todo", str(exc))
    if isinstance(exc, BoardNotFoundError):
        # A board-level event records the board id in todo_activity.todo_id, which is
        # todo-centric and NOT NULL — semantically odd, though no integrity break
        # (todo_id is plain Text, no FK). Board-level events have no column of their own.
        await _audit_attempt(
            sctx, entity="board", action=action, outcome="denied", todo_id=str(exc)
        )
        return _not_found("board", str(exc))
    # ValueError / TypeError → an error attempt against the tool's primary entity.
    await _audit_attempt(sctx, entity=entity, action=action, outcome="error")
    return _invalid(str(exc))


# --------------------------------------------------------------------------- #
# UI resource: the board, an MCP Apps view.                                    #
# --------------------------------------------------------------------------- #


#: The board UI's listed resource URI. A host placement names it (NimbleBrain's sidebar, in the
#: manifest), so it stays fixed across builds.
BOARD_UI_URI = "ui://todos/main"

#: The board HTML, read once at start. Every ``ui://todos/main`` URI serves this one copy, and
#: :data:`BOARD_UI_BUILD` is the hash of exactly these bytes, so a key always names what is served.
BOARD_UI_HTML = load_ui()

#: The first 12 hex characters of the served HTML's SHA-256.
BOARD_UI_BUILD = hashlib.sha256(BOARD_UI_HTML.encode("utf-8")).hexdigest()[:12]

#: The URI the tools bind to. A host may cache a ``ui://`` resource by URI, so the URI carries
#: the build: a new UI is a new URI, and a host fetches it instead of rendering its cached copy.
#: ``get_board_summary`` binds to it, so an MCP Apps host renders the board when the agent reads
#: the board's health, and ``get_todo`` binds to it, so the host renders that to-do's detail.
BOARD_UI_BOUND_URI = f"{BOARD_UI_URI}/{BOARD_UI_BUILD}"

# The board is one self-contained file: it loads nothing from elsewhere, so its frame's
# policy allows no outside origin. It asks for a border, which a host draws where the frame sits
# among other content (inline in a conversation); a host gives a full pane no border.
_BOARD_UI_APP = AppConfig(prefers_border=True, csp=ResourceCSP())


@mcp.resource(BOARD_UI_URI, app=_BOARD_UI_APP, meta=CHATGPT_UI_META)
def todos_ui() -> str:
    """The To-dos board UI: static HTML, holding no data, that an MCP Apps host renders in a frame.

    Serving it reads a file and opens no database session, so the board loads (and can show
    an error) even when the database cannot be reached. Every datum it shows is fetched at
    runtime through the host's tool calls, which carry the caller's identity.
    """
    return BOARD_UI_HTML


# Any build key resolves to the current UI, so a conversation or a tool list that names an
# older build still renders the board. The key selects nothing and is never echoed.
@mcp.resource(f"{BOARD_UI_URI}/{{build}}", app=_BOARD_UI_APP, meta=CHATGPT_UI_META)
def todos_ui_build(build: str) -> str:
    """The To-dos app UI under a build key. Every key serves the current build."""
    del build
    return BOARD_UI_HTML


# --------------------------------------------------------------------------- #
# Create / plan                                                                 #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ADD, meta=CHATGPT_TOOL_META)
async def create_todo(
    title: str,
    details: str | None = None,
    board_id: str | None = None,
    parent_id: str | None = None,
    priority: str | None = None,
    labels: list[str] | None = None,
    assignee: str | None = None,
    due_at: datetime | None = None,
    status: Status | None = None,
    blocked_reason: str | None = None,
    idempotency_key: str | None = None,
    source: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Add one to-do. For several, or one goal broken into steps, use plan_todos instead.

    Omit ``board_id`` and it lands on the workspace's default board, created on first use.
    It starts ``open`` unless ``status`` says otherwise; after that its status moves only
    through start_todo, complete_todo, block_todo, reopen_todo and cancel_todo.

    Args:
        title: To-do title (required).
        details: Optional markdown detail.
        board_id: Target board, its id or its key (e.g. MAG); defaults to the auto-created
            workspace default board.
        parent_id: Optional parent to-do (id or key, e.g. MAG-3) for step decomposition.
        priority: urgent, high, normal (the default) or low. Any other value is kept and
            ranks after low.
        labels: Open tags.
        assignee: Who it is for: agent, a person's handle, or any name.
        due_at: Optional due time (ISO-8601).
        status: The starting status; default open. done and cancelled set completed_at;
            blocked needs blocked_reason. A board's lanes are views over status, so there
            is no column argument.
        blocked_reason: Why it is blocked. Required when status is blocked, and taken only
            then.
        idempotency_key: Optional retry-dedupe token. A retried create with the SAME
            key on the SAME board converges to one row instead of duplicating.
        source: Provenance (agent / user / import).

    Returns:
        {todo, action: "created"|"matched", next_step}.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj, action = await store.create_todo(
                conn,
                sctx,
                title=title,
                details=details,
                board_id=board_id,
                parent_id=parent_id,
                priority=priority,
                labels=labels,
                assignee=assignee,
                due_at=due_at,
                status=status,
                blocked_reason=blocked_reason,
                idempotency_key=idempotency_key,
                source=source,
                limits=runtime().limits(sctx),
            )
    except (
        BoardNotFoundError,
        TodoNotFoundError,
        LimitExceededError,
        ValueError,
        TypeError,
    ) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="create", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="create_todo")
    if action == "created":
        runtime().record(TodoEvent.CREATED)
        # A matched create is a replay that changed no to-do, so there is nothing to announce.
        await announce_resource_list_changed(ctx)
    return {
        "todo": dump(todo_obj),
        "action": action,
        "next_step": (
            f"Start it with start_todo(todo_id={todo_obj.key!r}), or pull the next item with "
            "get_next_todo()."
        ),
    }


@mcp.tool(annotations=ADD, meta=CHATGPT_TOOL_META)
async def plan_todos(
    todos: list[PlanItem],
    board_id: str | None = None,
    parent_id: str | None = None,
    idempotency_key: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Add several to-dos in one call: a plan, or one goal broken into steps.

    For a single to-do use create_todo. It only adds: the to-dos already on the board are
    never replaced or removed, so it is safe on a board a person also edits. Pass
    ``parent_id`` to group the steps under a goal, and ``idempotency_key`` so a retried call
    returns the same to-dos instead of adding them twice.

    Args:
        todos: The steps, in order, each {title, details?, priority?, labels?, due_at?,
            status?, blocked_reason?}.
        board_id: Target board, its id or its key (e.g. MAG); defaults to the workspace
            default board (auto-created).
        parent_id: Optional shared parent to-do (id or key) grouping every step under one goal.
        idempotency_key: Optional call-level key; per-item keys derive from it + index
            so a re-run of the plan converges.

    Returns:
        {todos, created, next_step}. Each row carries a ``details_preview`` in place of
        ``details``; read a whole to-do with get_todo.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            created_todos, created = await store.plan_todos(
                conn,
                sctx,
                todos=todos,
                board_id=board_id,
                parent_id=parent_id,
                idempotency_key=idempotency_key,
                limits=runtime().limits(sctx),
            )
    except (
        BoardNotFoundError,
        TodoNotFoundError,
        LimitExceededError,
        ValueError,
        TypeError,
    ) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="plan", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="plan_todos")
    runtime().record(TodoEvent.CREATED, created)
    await announce_resource_list_changed(ctx)
    return {
        "todos": [_row(t) for t in created_todos],
        "created": created,
        "next_step": (
            "Pull the first item with get_next_todo(), or review the board with get_board_summary(). "
            f"{_READ_HINT}"
        ),
    }


# --------------------------------------------------------------------------- #
# Fields                                                                        #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def update_todo(
    todo_id: str,
    title: str | None = None,
    details: str | None = None,
    priority: str | None = None,
    labels: list[str] | None = None,
    assignee: str | None = None,
    due_at: datetime | None = None,
    parent_id: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Change a to-do's title, details, priority, labels, assignee, due date or goal.

    Not its status: to move a to-do, use start_todo, complete_todo, block_todo,
    reopen_todo or cancel_todo. Only the fields you pass change. Null or an empty value
    leaves a field as it is, so no field can be cleared here.

    Args:
        todo_id: The to-do to update (required): its id or its key (e.g. MAG-3).
        title: The new title. Omit to keep it.
        details: New markdown details, replacing the old. Omit to keep them.
        priority: urgent, high, normal or low; any other value is kept and ranks after low.
            Omit to keep it.
        labels: The whole new label list, replacing the old. Omit (or pass an empty list)
            to keep the labels.
        assignee: Who it is for: agent, a person's handle, or any name. Omit to keep it.
        due_at: The new due time (ISO-8601). Omit to keep it.
        parent_id: The goal this to-do becomes a step of, by id or key; not the to-do
            itself. Omit to keep it.

    Returns:
        {todo, next_step}.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj = await store.update_todo(
                conn,
                sctx,
                todo_id=todo_id,
                title=title,
                details=details,
                priority=priority,
                labels=labels,
                assignee=assignee,
                due_at=due_at,
                parent_id=parent_id,
                limits=runtime().limits(sctx),
            )
    except (TodoNotFoundError, LimitExceededError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="update", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_todo")
    await announce_resource_list_changed(ctx)
    return {
        "todo": dump(todo_obj),
        "next_step": (
            f"Review it with get_todo(todo_id={todo_obj.key!r}), or move it with "
            "start_todo / complete_todo."
        ),
    }


# --------------------------------------------------------------------------- #
# Transitions (encode side effects; self-documenting for the model)             #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=MOVE, meta=CHATGPT_TOOL_META)
async def start_todo(todo_id: str, ctx: Context | None = None) -> dict:
    """Mark a to-do in progress: work on it has begun.

    Clears any ``blocked_reason``, so it also resumes a blocked to-do once the blocker is
    gone. When the work is finished use complete_todo; when it is stuck, block_todo.

    Args:
        todo_id: The to-do to start (required, in-scope): its id or its key (e.g. MAG-3).

    Returns:
        {todo, previous_status, next_step}. Not-found (not 403) if out of scope.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj, previous_status = await store.start_todo(
                conn, sctx, todo_id=todo_id, limits=runtime().limits(sctx)
            )
    except (TodoNotFoundError, LimitExceededError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="start", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="start_todo")
    await announce_resource_list_changed(ctx)
    return {
        "todo": dump(todo_obj),
        "previous_status": previous_status,
        "next_step": (
            f"Complete it with complete_todo(todo_id={todo_obj.key!r}) when done, or block it "
            "with block_todo if you hit a blocker."
        ),
    }


@mcp.tool(annotations=MOVE_STAMPED, meta=CHATGPT_TOOL_META)
async def complete_todo(todo_id: str, ctx: Context | None = None) -> dict:
    """Mark a to-do done: the work is finished. Sets ``completed_at``.

    If it will not be done, use cancel_todo instead; to only hide it, archive_todo.
    reopen_todo undoes it.

    Args:
        todo_id: The to-do to complete (required, in-scope): its id or its key (e.g. MAG-3).

    Returns:
        {todo, previous_status, next_step}. Not-found (not 403) if out of scope.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj, previous_status = await store.complete_todo(conn, sctx, todo_id=todo_id)
    except (TodoNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="complete", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="complete_todo")
    runtime().record(TodoEvent.COMPLETED)
    await announce_resource_list_changed(ctx)
    return {
        "todo": dump(todo_obj),
        "previous_status": previous_status,
        "next_step": "Pull the next item with get_next_todo(), or review progress with get_board_summary().",
    }


@mcp.tool(annotations=MOVE, meta=CHATGPT_TOOL_META)
async def block_todo(todo_id: str, reason: str, ctx: Context | None = None) -> dict:
    """Mark a to-do blocked: it is waiting on something outside it (a person, a review).

    Records ``reason`` as ``blocked_reason``; get_next_todo skips blocked to-dos. When the
    blocker clears, start_todo resumes it, or reopen_todo puts it back to open.

    Args:
        todo_id: The to-do to block (required, in-scope): its id or its key (e.g. MAG-3).
        reason: Why it is blocked (required) — stored as ``blocked_reason``.

    Returns:
        {todo, previous_status, next_step}. Not-found (not 403) if out of scope.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj, previous_status = await store.block_todo(
                conn, sctx, todo_id=todo_id, reason=reason, limits=runtime().limits(sctx)
            )
    except (TodoNotFoundError, LimitExceededError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="block", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="block_todo")
    runtime().record(TodoEvent.BLOCKED)
    await announce_resource_list_changed(ctx)
    return {
        "todo": dump(todo_obj),
        "previous_status": previous_status,
        "next_step": (
            f"Unblock it with start_todo(todo_id={todo_obj.key!r}) once cleared, or move on with "
            "get_next_todo()."
        ),
    }


@mcp.tool(annotations=MOVE, meta=CHATGPT_TOOL_META)
async def reopen_todo(todo_id: str, ctx: Context | None = None) -> dict:
    """Put a to-do back to open: it was done, cancelled or blocked, and is to do again.

    Clears ``completed_at`` and ``blocked_reason``. Use it rather than create_todo when
    finished work comes back; to resume work right away, use start_todo.

    Args:
        todo_id: The to-do to reopen (required, in-scope): its id or its key (e.g. MAG-3).

    Returns:
        {todo, previous_status, next_step}. Not-found (not 403) if out of scope.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj, previous_status = await store.reopen_todo(
                conn, sctx, todo_id=todo_id, limits=runtime().limits(sctx)
            )
    except (TodoNotFoundError, LimitExceededError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="reopen", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="reopen_todo")
    await announce_resource_list_changed(ctx)
    return {
        "todo": dump(todo_obj),
        "previous_status": previous_status,
        "next_step": f"Start it again with start_todo(todo_id={todo_obj.key!r}).",
    }


@mcp.tool(annotations=MOVE_STAMPED, meta=CHATGPT_TOOL_META)
async def cancel_todo(todo_id: str, ctx: Context | None = None) -> dict:
    """Mark a to-do cancelled: it will not be done. Sets ``completed_at``; takes no reason.

    If the work was finished, use complete_todo instead; to hide a to-do without changing
    its status, archive_todo. reopen_todo undoes it.

    Args:
        todo_id: The to-do to cancel (required, in-scope): its id or its key (e.g. MAG-3).

    Returns:
        {todo, previous_status, next_step}. Not-found (not 403) if out of scope.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj, previous_status = await store.cancel_todo(conn, sctx, todo_id=todo_id)
    except (TodoNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="cancel", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="cancel_todo")
    await announce_resource_list_changed(ctx)
    return {
        "todo": dump(todo_obj),
        "previous_status": previous_status,
        "next_step": "Pull the next item with get_next_todo().",
    }


# --------------------------------------------------------------------------- #
# Order / cleanup                                                               #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def reorder_todos(
    board_id: str,
    status: Status,
    todo_ids: list[str],
    ctx: Context | None = None,
) -> dict:
    """Set the order of the to-dos in one status lane of a board, to reprioritize them.

    It changes positions only. To move a to-do to another status, use start_todo,
    complete_todo, block_todo, reopen_todo or cancel_todo.

    Args:
        board_id: The board whose lane is reordered (required, in-scope): its id or its key.
        status: The lane to reorder, by its status.
        todo_ids: Every to-do in the lane (ids or keys), in the new order.

    Returns:
        {ordered, next_step}.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            ordered = await store.reorder_todos(
                conn, sctx, board_id=board_id, status=status, todo_ids=todo_ids
            )
    except (BoardNotFoundError, TodoNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="reorder", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="reorder_todos")
    await announce_resource_list_changed(ctx)
    return {
        "ordered": ordered,
        "next_step": f"Review the lane with list_todos(board_id={board_id!r}, status={status!r}).",
    }


@mcp.tool(annotations=HIDE, meta=CHATGPT_TOOL_META)
async def archive_todo(todo_id: str, ctx: Context | None = None) -> dict:
    """Hide a to-do from default views, leaving its status as it is.

    Archiving is not finishing: a finished to-do is complete_todo, one that will not be
    done is cancel_todo. Nothing un-archives a to-do; list_todos(include_archived=True)
    still finds it.

    Args:
        todo_id: The to-do to archive (required, in-scope): its id or its key (e.g. MAG-3).

    Returns:
        {todo, next_step}. Not-found (not 403) if out of scope.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj = await store.archive_todo(conn, sctx, todo_id=todo_id)
    except (TodoNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="archive", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="archive_todo")
    await announce_resource_list_changed(ctx)
    return {
        "todo": dump(todo_obj),
        "next_step": "It is hidden from default views; list with include_archived=True to see it.",
    }


# --------------------------------------------------------------------------- #
# Reads                                                                         #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_todos(
    board_id: str | None = None,
    status: Status | None = None,
    assignee: str | None = None,
    label: str | None = None,
    parent_id: str | None = None,
    include_archived: bool = False,
    query: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """List or search to-dos, with optional filters and cursor pagination.

    Without ``query`` the newest come first. With ``query``, only to-dos whose title or
    details contain every word of it are listed, best match first: exact title, then
    title starting with the query, then every word starting a title word, then the rest,
    newest first within each. Rows carry a short ``details_preview``, not ``details``:
    pick from the previews, then read the to-do you act on whole with ``get_todo``.

    Args:
        board_id: Filter to one board, by its id or its key (e.g. MAG).
        status: Only to-dos in this status.
        assignee: Filter by assignee.
        label: Filter to to-dos carrying this label.
        parent_id: Filter to a goal's steps (the goal's id or key).
        include_archived: Include archived to-dos (default False hides them).
        query: Search text over title and details, split on whitespace; every word must
            match, case-insensitively, as a substring. Combines with the other filters.
        limit: Page size (default 50, clamped to 200).
        cursor: Opaque pagination cursor from a previous page of the same call; pass the
            same filters and query with it.

    Returns:
        {todos, next_cursor, next_step}. Each row carries a ``details_preview`` in place
        of ``details``; read a whole to-do with get_todo.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todos_page, next_cursor = await store.list_todos(
                conn,
                sctx,
                board_id=board_id,
                status=status,
                assignee=assignee,
                label=label,
                parent_id=parent_id,
                include_archived=include_archived,
                query=query,
                limit=limit,
                cursor=cursor,
            )
    except (ValueError, TypeError) as exc:
        await _audit_attempt(sctx, entity="todo", action="list", outcome="error")
        return _invalid(f"invalid cursor or filter: {exc}")
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_todos")
    if next_cursor is not None:
        next_step = f"Fetch the next page with list_todos(cursor={next_cursor!r}, ...)."
    else:
        next_step = "This is the last page."
    if todos_page:
        next_step = f"{next_step} {_READ_HINT}"
    elif query and query.strip() and not cursor:
        next_step = "No match. Retry with fewer or different words, or list without a query."
    return {
        "todos": [_row(t) for t in todos_page],
        "next_cursor": next_cursor,
        "next_step": next_step,
    }


# An MCP Apps host renders the to-do's detail in the board UI for this tool, as it renders the
# board for get_board_summary. The UI also calls it from its frame, so both audiences may.
@mcp.tool(
    annotations=READ,
    meta=CHATGPT_TOOL_META,
    app=AppConfig(resource_uri=BOARD_UI_BOUND_URI, visibility=["model", "app"]),
)
async def get_todo(todo_id: str, ctx: Context | None = None) -> dict:
    """Get a to-do with its steps, counts, and recent activity timeline.

    The to-do comes whole, ``details`` included. Each step is a row with a
    ``details_preview`` in place of ``details``; read a step whole with its own get_todo.

    Args:
        todo_id: The to-do's id or its key (e.g. MAG-3), required.

    Returns:
        {todo, steps, counts, recent_activity, next_step}. Not-found (not 403)
        if out of scope.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj, steps, counts, recent_activity = await store.get_todo(
                conn, sctx, todo_id=todo_id
            )
    except TodoNotFoundError as exc:
        return await _handle_tool_error(sctx, entity="todo", action="get", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_todo")
    next_step = (
        f"Move it with start_todo(todo_id={todo_obj.key!r}), or update its fields with "
        f"update_todo(todo_id={todo_obj.key!r}, ...)."
    )
    if steps:
        next_step = (
            f"{next_step} Each step carries a details_preview, not the details: read a step "
            "whole with get_todo(todo_id=<the step's id>)."
        )
    return {
        "todo": dump(todo_obj),
        "steps": [_row(t) for t in steps],
        "counts": counts,
        "recent_activity": [dump(a) for a in recent_activity],
        "next_step": next_step,
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def get_next_todo(board_id: str | None = None, ctx: Context | None = None) -> dict:
    """The to-do to work on next: the highest-priority open, unarchived to-do.

    Blocked, in-progress and finished to-dos are skipped. Use it for "what should I do
    next" rather than listing to-dos and choosing.

    Args:
        board_id: Restrict to one board, by its id or its key (e.g. MAG); omit to consider
            every actionable to-do across all boards in the workspace.

    Returns:
        {todo | null, next_step}.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            todo_obj = await store.get_next_todo(conn, sctx, board_id=board_id)
            empty = todo_obj is None and not await store.has_todos(conn, sctx)
            has_board = bool(await store.list_boards(conn, sctx)) if empty else True
    except (BoardNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="todo", action="next", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_next_todo")
    if todo_obj is None:
        return {
            "todo": None,
            "next_step": (
                _onboard(has_board=has_board)
                if empty
                else "No open to-do to pick up. Review what is in progress or blocked with "
                "get_board_summary(), or lay out more work with plan_todos."
            ),
        }
    return {
        "todo": dump(todo_obj),
        "next_step": f"Start it with start_todo(todo_id={todo_obj.key!r}).",
    }


# The tool an MCP Apps host renders the board for. The board also calls it from its frame,
# so both audiences may.
@mcp.tool(
    annotations=READ,
    meta=CHATGPT_TOOL_META,
    app=AppConfig(resource_uri=BOARD_UI_BOUND_URI, visibility=["model", "app"]),
)
async def get_board_summary(board_id: str | None = None, ctx: Context | None = None) -> dict:
    """A board's health: per-lane counts, totals, and what needs attention.

    A lane with a WIP limit says whether it is within it; attention lists the overdue,
    stalled and blocked to-dos. Start here to report on a board, then name the to-dos behind the counts.

    Args:
        board_id: The board to summarize, its id or its key (e.g. MAG); omit for the
            workspace default board.

    Returns:
        {board, lanes, totals, attention, next_step}.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            board_obj, lanes, totals, attention = await store.get_board_summary(
                conn, sctx, board_id=board_id
            )
    except (BoardNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="board", action="summary", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_board_summary")
    if board_obj is None:
        # No board yet: an empty summary, not an error, and nothing is created by reading.
        return {
            "board": None,
            "lanes": lanes,
            "totals": totals,
            "attention": attention,
            "next_step": _onboard(has_board=False),
        }
    return {
        "board": dump(board_obj),
        "lanes": lanes,
        "totals": totals,
        "attention": attention,
        "next_step": "Pull the next item with get_next_todo(), or open a lane with list_todos(status=...).",
    }


# --------------------------------------------------------------------------- #
# Boards                                                                        #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_boards(ctx: Context | None = None) -> dict:
    """List the workspace's boards, each with its id, key and lanes.

    Use it to find a board's id or key. For one board's counts and what needs attention
    use get_board_summary; for the to-dos on it, list_todos.

    Returns:
        {boards, next_step}.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            boards = await store.list_boards(conn, sctx)
    except (ValueError, TypeError) as exc:
        await _audit_attempt(sctx, entity="board", action="list", outcome="error")
        return _invalid(str(exc))
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_boards")
    limits = runtime().limits(sctx)
    if not boards:
        next_step = _onboard(has_board=False)
    elif limits is not None and limits.max_boards is not None and len(boards) >= limits.max_boards:
        # The account cannot make another board, so the agent is pointed at the ones it has.
        next_step = _use_the_boards(boards)
    else:
        next_step = (
            "Open a board with get_board_summary(board_id=...), or create one with "
            "create_board(...)."
        )
    return {"boards": [dump(b) for b in boards], "next_step": next_step}


@mcp.tool(annotations=ADD, meta=CHATGPT_TOOL_META)
async def create_board(
    name: str,
    description: str | None = None,
    columns: list[dict] | None = None,
    key: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Create a board. ``columns`` defaults to the four canonical lanes.

    The board's key prefixes its to-dos' keys (MAG-1, MAG-2, ...) and cannot change later.
    Tell the person the key the result carries.

    Args:
        name: Board name (required).
        description: Optional description.
        columns: Ordered presentation overlay [{key, label, status, color?, wip_limit?}];
            defaults to Open / Doing / Blocked / Done.
        key: The board's key: 2-5 letters A-Z or digits, starting with a letter, unused in
            the workspace. Omit to derive one from the name ("Launch plan" → LP).

    Returns:
        {board, next_step}. ``board.key`` is the key chosen.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            board_obj = await store.create_board(
                conn,
                sctx,
                name=name,
                description=description,
                columns=columns,
                key=key,
                limits=runtime().limits(sctx),
            )
    except (LimitExceededError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="board", action="create", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="create_board")
    await announce_resource_list_changed(ctx)
    return {
        "board": dump(board_obj),
        "next_step": (
            f"The board's key is {board_obj.key}: its to-dos will be {board_obj.key}-1, "
            f"{board_obj.key}-2, ... Tell the person, then add to-dos with "
            f"create_todo(board_id={board_obj.key!r}, ...)."
        ),
    }


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def update_board(
    board_id: str,
    name: str | None = None,
    description: str | None = None,
    columns: list[dict] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Change a board's name, description or lanes. Its key never changes.

    Only the fields you pass change. Null or an empty name or description leaves it as
    it is, so neither can be cleared here. Editing ``columns`` changes which status each
    lane shows; no to-do's status changes.

    Args:
        board_id: The board to update (required, in-scope): its id or its key (e.g. MAG).
        name: The new name. Omit to keep it.
        description: The new description. Omit to keep it.
        columns: The whole new lane list, replacing the old: [{key, label, status, color?,
            wip_limit?}], each lane showing the to-dos in one status. Omit to keep the
            lanes; an empty list leaves the board with none.

    Returns:
        {board, next_step}.
    """
    try:
        sctx = runtime().identity()
    except AuthError as exc:
        return _auth_error(exc)
    try:
        async with runtime().session(sctx) as conn:
            board_obj = await store.update_board(
                conn, sctx, board_id=board_id, name=name, description=description, columns=columns
            )
    except (BoardNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="board", action="update", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_board")
    await announce_resource_list_changed(ctx)
    return {
        "board": dump(board_obj),
        "next_step": f"Review the board with get_board_summary(board_id={board_obj.key!r}).",
    }


# --------------------------------------------------------------------------- #
# Prompts                                                                       #
# --------------------------------------------------------------------------- #

mcp.prompt(
    title="Get started with your to-dos",
    description=(
        "Start your to-do list, or pick it up: plan your first to-dos, or see what is in "
        "progress and what is next."
    ),
)(prompts.get_started)


# --------------------------------------------------------------------------- #
# ASGI entrypoint                                                               #
# --------------------------------------------------------------------------- #

_app: Any = None


def __getattr__(name: str) -> Any:
    """``app``: the ASGI app for the runtime ``TODOS_RUNTIME`` names, built on first access.

    Built lazily so importing this module needs no runtime; ``uvicorn mcp_todos.server:app``
    resolves it, and refuses to start when no runtime can be selected.
    """
    global _app
    if name == "app":
        if _app is None:
            _app = installed().wrap_http(mcp.http_app())
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
