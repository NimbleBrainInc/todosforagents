"""To-dos data-access layer — CRUD + transitions + plan/idempotency logic.

This module is the **only door** to the To-dos database: nothing else reads its rows.
Every function:

  * takes ``conn`` — an :class:`AsyncConnection` already inside
    the runtime's session: on Postgres every query runs under RLS scoped to one
    ``(tenant_id, workspace_id)``, so a forgotten ``WHERE`` cannot leak; on SQLite
    there is exactly one principal.
  * takes ``ctx`` — the verified :class:`~mcp_todos.runtime.Principal`. Identity columns
    (``tenant_id`` / ``workspace_id``) are stamped from ``ctx`` on every insert
    (the RLS ``WITH CHECK`` validates the active workspace); they are NEVER read
    from a tool argument, and NEVER returned on the wire.
  * returns Pydantic **wire models** (via ``Model.from_row``), never raw rows —
    ``tenant_id`` / ``workspace_id`` never cross the boundary.
  * records access on a split path. **Mutations** append a durable
    :data:`todo_activity` row via :func:`write_audit` — the change record.
    **Denied/error attempts** (including failed reads, written from the server
    layer) also land in the table. **Successful reads** do NOT write a table row:
    they emit a structured ``todos_access`` log line instead, avoiding write
    amplification on a read-heavy loop and an unbounded table.

The load-bearing contracts:

  * **Idempotent create.** ``create_todo`` / ``plan_todos`` with an
    ``idempotency_key`` converge to one row under the partial unique index
    ``uq_todo_idem`` (``(tenant_id, workspace_id, board_id, idempotency_key)``),
    even under a concurrent double-insert. We do NOT do a race-y check-then-insert:
    ``INSERT ... ON CONFLICT DO NOTHING`` against the dedupe index then re-select,
    so a lost race resolves to the row the winner inserted — one row, never a
    duplicate, never a 500. See :func:`_upsert_returning`.
  * **Keys and numbers.** Every board has a fixed ``key`` and every to-do a ``number``,
    sequential per board and never reused; ``MAG-3`` names a to-do as well as its
    ``td_<ulid>`` id does. A create claims the board's ``next_number`` under a row lock in
    its own transaction (:func:`_claim_number`), and an idempotent replay claims nothing.
    Every argument that takes a to-do id resolves a key first (:func:`_resolve_todo`),
    and every one that takes a board id resolves a board key (:func:`_board_by_ref`,
    :func:`_on_board`), within the caller's workspace.
  * **Default board (zero-ceremony start).** A workspace gets a default
    board auto-created on first ``create_todo``/``plan_todos`` with no ``board_id``.
    Exactly one default per workspace, enforced by the partial unique index
    ``uq_board_default``; the auto-create handles that race the same ON CONFLICT
    DO NOTHING + re-select way. See :func:`_resolve_default_board`.

Reads (``list_todos`` / ``get_todo`` / ``get_next_todo`` / ``get_board_summary``) are
side-effect free — they return wire models, emit a ``todos_access`` log line (never
a ``todo_activity`` row), and never auto-create a board.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pgkeyset import Listing, Sort
from pydantic import ValidationError
from sqlalchemy import (
    Integer,
    RowMapping,
    Select,
    and_,
    case,
    func,
    literal,
    or_,
    select,
    tuple_,
    type_coerce,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_todos.db.dialect import insert_for, json_array_contains
from mcp_todos.db.tables import board, todo, todo_activity
from mcp_todos.ids import new_board_id, new_todo_id
from mcp_todos.keys import (
    DEFAULT_BOARD_KEY,
    derive_board_key,
    normalize_board_key,
    parse_board_key,
    parse_todo_key,
)
from mcp_todos.models import STATUSES, Board, PlanItem, Todo, TodoActivity
from mcp_todos.runtime import Principal

__all__ = [
    "BoardNotFoundError",
    "TodoNotFoundError",
    "archive_todo",
    "block_todo",
    "cancel_todo",
    "complete_todo",
    "create_board",
    "create_todo",
    "get_board_summary",
    "get_next_todo",
    "get_todo",
    "list_boards",
    "list_todos",
    "plan_todos",
    "reopen_todo",
    "reorder_todos",
    "start_todo",
    "update_board",
    "update_todo",
    "write_audit",
]


class TodoNotFoundError(LookupError):
    """A to-do id is not visible in this (tenant, workspace) — 404, not 403.

    Raised for both genuinely-absent ids and out-of-scope ids (RLS returns
    nothing): the caller can never tell the difference, so there is no existence
    oracle.
    """


class BoardNotFoundError(LookupError):
    """A board id is not visible in this (tenant, workspace) — 404, not 403."""


# --------------------------------------------------------------------------- #
# Canonical status set + default board columns             #
# --------------------------------------------------------------------------- #
#
# The canonical status set is the agent contract. It is validated APP-SIDE with
# graceful fallthrough — NOT a DB CHECK/enum, which would force a
# migration to add a state. An unknown status passed to create_todo raises
# ValueError (→ invalid_request at the tool boundary) rather than being silently
# stored; the transition verbs only ever set members of this set.
_CANONICAL_STATUSES = STATUSES
_DEFAULT_PRIORITY = "normal"
_DEFAULT_STATUS = "open"

# Terminal statuses — a to-do here is finished and drops out of the "needs
# attention" surfaces (overdue) and the active-lane totals. ``cancelled`` is also
# hidden from default board lanes.
_TERMINAL_STATUSES = ("done", "cancelled")

# Priority rank for get_next_todo ordering: urgent > high > normal > low; any unknown
# (open) priority sorts LAST. A
# LOWER rank sorts first under ``ORDER BY rank ASC``.
_PRIORITY_RANK: dict[str, int] = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
_PRIORITY_RANK_UNKNOWN = 4  # any priority outside the known set ranks after `low`

# A to-do in_progress whose ``updated_at`` is older than this is "stalled" — work
# that was picked up and then went quiet.
_STALLED_AFTER_DAYS = 3

# Default + ceiling page sizes for the list/search reads.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

# How many recent todo_activity rows get_todo surfaces (reverse-chronological).
_RECENT_ACTIVITY_LIMIT = 20

# The four canonical lanes a board is born with. Each column maps
# 1:1 to a canonical status in v1; `cancelled` is hidden from board lanes by
# default and so has no lane.
_DEFAULT_COLUMNS: list[dict[str, Any]] = [
    {"key": "open", "label": "Open", "status": "open"},
    {"key": "doing", "label": "Doing", "status": "in_progress"},
    {"key": "blocked", "label": "Blocked", "status": "blocked"},
    {"key": "done", "label": "Done", "status": "done"},
]
_DEFAULT_BOARD_NAME = "To-dos"


# --------------------------------------------------------------------------- #
# Input size caps (MVP row-bloat insurance)                                    #
# --------------------------------------------------------------------------- #
#
# The columns stay open ``Text`` (NO DB CHECK — a CHECK is schema rigidity and a
# migration every time a cap moves). We enforce length app-side
# at the start of every write so an over-length value raises ``ValueError`` —
# which the tool layer maps to the structured ``invalid_request`` shape — rather
# than silently bloating a row. The load-bearing cap is ``details`` (the only
# unbounded free-text store, 64 KiB markdown); the rest are cheap insurance.

_CAP_TITLE = 512
_CAP_SHORT = 256  # the short open strings: status / priority / assignee / source
_CAP_REASON = 1024  # block_todo's blocked_reason
_CAP_LABEL = 256  # one label tag
_CAP_KEY = 256  # idempotency_key
_CAP_BOARD_NAME = 512
_CAP_DESCRIPTION = 2048
_CAP_DETAILS = 65536  # 64 KiB markdown detail — the load-bearing cap
_CAP_PLAN_ITEMS = 100  # max steps in one plan_todos call — bounds the per-item insert loop


def _check_len(field: str, value: str | None, limit: int) -> None:
    """Raise ``ValueError`` if ``value`` exceeds ``limit`` UTF-8 bytes.

    A no-op for ``None``. The caps express a row-bloat budget in bytes/KiB, so the
    check measures the UTF-8 byte length — a multibyte string can be up to ~4×
    its character count. The message names the field and the limit but never the
    value, so an over-length input fails as a clean ``invalid_request`` at the
    tool boundary instead of a DB write that bloats a row.
    """
    if value is not None and len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field} exceeds the maximum length of {limit} bytes")


# --------------------------------------------------------------------------- #
# Audit                                                                        #
# --------------------------------------------------------------------------- #

# Successful reads emit a structured access log here rather than a table row.
# Every read (list_boards / list_todos / get_todo / get_next_todo / get_board_summary)
# routes through it; mutations write a todo_activity row instead.
_access_logger = logging.getLogger("mcp_todos.audit")


def _log_access(
    ctx: Principal,
    *,
    action: str,
    todo_id: str | None = None,
) -> None:
    """Emit one structured ``todos_access`` log line for a SUCCESSFUL read.

    Successful reads do not write a :data:`todo_activity` table row (that would be
    write amplification on a read-heavy agent loop and an unbounded table); they
    log instead. The line carries the verified scope and identifiers from ``ctx``
    so the access is still attributable. Denied/error attempts and all mutations
    still go to the durable table.
    """
    _access_logger.info(
        "todos_access",
        extra={
            "tenant_id": ctx.tenant_id,
            "workspace_id": ctx.workspace_id,
            "action": action,
            "todo_id": todo_id,
            "correlation_id": ctx.correlation_id,
        },
    )


async def write_audit(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    todo_id: str,
    action: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one :data:`todo_activity` row — the durable change/security record.

    Called for **mutations** (outcome ``ok``, from this module) and for
    **denied/error attempts** (from the server layer, including failed reads).
    Successful reads do NOT call this — they emit a structured access log via
    :func:`_log_access` instead (no table row).

    Identity (``tenant_id`` / ``workspace_id`` / ``user_id`` / ``correlation_id``)
    comes only from ``ctx``. The row is written inside the caller's already-scoped
    transaction, so it is subject to the same RLS policy as the data it describes.
    ``outcome`` ∈ {``ok``, ``denied``, ``error``}; ``action`` ∈ {``create``,
    ``plan``, ``update``, ``start``, ``complete``, ``block``, ``reopen``,
    ``cancel``, ``reorder``, ``archive``}.

    Optional ``detail`` stores structured context for the change in the JSONB
    ``detail`` column. It must carry ONLY ids, counts, and field NAMES — never
    PII/values.
    """
    await conn.execute(
        todo_activity.insert().values(
            tenant_id=ctx.tenant_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            todo_id=todo_id,
            action=action,
            outcome=outcome,
            correlation_id=ctx.correlation_id,
            detail=detail,
        )
    )


# --------------------------------------------------------------------------- #
# Small internal helpers                                                       #
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(UTC)


def _clean(value: str | None) -> str | None:
    """Trim a free-text value; treat empty/whitespace as ``None``."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Coerce a tz-naive datetime to UTC; pass tz-aware (and ``None``) through.

    A client may send a tz-naive ``due_at``. Without this, Postgres interprets it
    in the session TZ, silently shifting the instant. We pin the contract: naive
    means UTC.
    """
    return dt.replace(tzinfo=UTC) if (dt is not None and dt.tzinfo is None) else dt


def _clean_labels(labels: list[str] | None) -> list[str] | None:
    """Trim + drop empties from a label list; ``None``/all-empty collapse to ``None``.

    Labels are open tags. We strip each, drop blanks, and cap each at ``_CAP_LABEL``.
    An absent OR empty list returns ``None`` → a no-op in a partial update. Clearing
    labels is NOT supported by the partial-update contract (omission is the only
    semantics, by design).
    """
    if labels is None:
        return None
    cleaned = [s.strip() for s in labels if isinstance(s, str) and s.strip()]
    for label in cleaned:
        _check_len("label", label, _CAP_LABEL)
    return cleaned or None


def _normalize_status(status: str | None) -> str:
    """Validate a create-time status against the canonical set; default ``open``.

    The canonical set is enforced app-side with graceful fallthrough —
    an unknown status is rejected (``ValueError`` → ``invalid_request``) rather
    than silently stored, so the agent contract stays the source of truth.
    ``None`` / empty defaults to ``open``.
    """
    cleaned = _clean(status)
    if cleaned is None:
        return _DEFAULT_STATUS
    if cleaned not in _CANONICAL_STATUSES:
        allowed = ", ".join(_CANONICAL_STATUSES)
        raise ValueError(f"status must be one of: {allowed} (got {cleaned!r})")
    return cleaned


def _create_side_effects(
    status: str, blocked_reason: str | None, now: datetime, *, field: str = "blocked_reason"
) -> dict[str, Any]:
    """The columns a to-do created in ``status`` carries, as its transition verb would set them.

    ``done`` and ``cancelled`` stamp ``completed_at``; ``blocked`` records a reason, which it
    requires. A reason with any other status is refused rather than stored where no verb
    would clear it. ``field`` names the argument in the error.
    """
    _check_len(field, blocked_reason, _CAP_REASON)
    reason = _clean(blocked_reason)
    if status == "blocked":
        if reason is None:
            raise ValueError(f"{field} is required when status is blocked")
        return {"blocked_reason": reason, "completed_at": None}
    if reason is not None:
        raise ValueError(f"{field} is only taken when status is blocked")
    return {"blocked_reason": None, "completed_at": now if status in _TERMINAL_STATUSES else None}


def _set_fields(side_effects: Mapping[str, Any]) -> dict[str, Any] | None:
    """An activity ``detail`` naming the side-effect columns a create set, or ``None``."""
    fields = [name for name, value in side_effects.items() if value is not None]
    return {"fields": fields} if fields else None


async def _one(conn: AsyncConnection, stmt: Select) -> RowMapping | None:
    return (await conn.execute(stmt)).mappings().first()


async def _all(conn: AsyncConnection, stmt: Select) -> Sequence[RowMapping]:
    return (await conn.execute(stmt)).mappings().all()


def _todos() -> Select:
    """``SELECT`` over to-dos with their board's key (``board_key``), which a to-do's
    displayed key is built from."""
    return select(todo, board.c.key.label("board_key")).select_from(
        todo.join(board, todo.c.board_id == board.c.id)
    )


async def _todo_row(conn: AsyncConnection, todo_id: str) -> RowMapping:
    """Load a to-do by its canonical id within scope; 404 (not 403) if absent/out-of-scope."""
    row = await _one(conn, _todos().where(todo.c.id == todo_id))
    if row is None:
        raise TodoNotFoundError(todo_id)
    return row


async def _key_lookup(conn: AsyncConnection, ctx: Principal, ref: str) -> RowMapping | None:
    """The to-do a key (``MAG-3``, any case) names in the caller's workspace, or ``None``.

    The workspace predicate keeps a key to the active workspace even where row-level
    security admits a wider read set: a key is unique only within one workspace.
    """
    parsed = parse_todo_key(ref)
    if parsed is None:
        return None
    board_key, number = parsed
    return await _one(
        conn,
        _todos().where(
            _in_workspace(ctx),
            board.c.key == board_key,
            todo.c.number == number,
        ),
    )


async def _resolve_todo(conn: AsyncConnection, ctx: Principal, ref: str) -> RowMapping:
    """Load a to-do by id or key; 404 (not 403) if neither names one in scope.

    An unknown key is the same not-found as an unknown id, carrying the reference as given.
    """
    if parse_todo_key(ref) is not None:
        row = await _key_lookup(conn, ctx, ref)
        if row is None:
            raise TodoNotFoundError(ref)
        return row
    return await _todo_row(conn, ref)


async def _claim_number(conn: AsyncConnection, board_id: str) -> int:
    """The board's next to-do number, read under a row lock held to the end of the
    transaction, so concurrent creates on one board take turns. SQLite takes the whole
    database's write lock at ``BEGIN IMMEDIATE`` instead. A create that inserts advances it
    with :func:`_advance_number`; one that matches an existing row leaves it."""
    stmt = select(board.c.next_number).where(board.c.id == board_id).with_for_update()
    return int((await conn.execute(stmt)).scalar_one())


async def _advance_number(conn: AsyncConnection, board_id: str, next_number: int) -> None:
    await conn.execute(board.update().where(board.c.id == board_id).values(next_number=next_number))


async def _board_row(conn: AsyncConnection, board_id: str) -> RowMapping:
    """Load a board by id within scope; 404 (not 403) if absent/out-of-scope."""
    row = await _one(conn, select(board).where(board.c.id == board_id))
    if row is None:
        raise BoardNotFoundError(board_id)
    return row


def _in_workspace(ctx: Principal) -> Any:
    """The caller's active workspace, for a key lookup: a key is unique only within one
    workspace, so it never resolves in a wider read set row-level security admits."""
    return and_(board.c.tenant_id == ctx.tenant_id, board.c.workspace_id == ctx.workspace_id)


async def _board_by_ref(conn: AsyncConnection, ctx: Principal, ref: str) -> RowMapping:
    """Load a board by id or key (``MAG``, any case) in one query; 404 (not 403) if neither
    names one in scope. An unknown key is the same not-found as an unknown id, carrying the
    reference as given."""
    key = parse_board_key(ref)
    if key is None:
        return await _board_row(conn, ref)
    row = await _one(conn, select(board).where(_in_workspace(ctx), board.c.key == key))
    if row is None:
        raise BoardNotFoundError(ref)
    return row


def _on_board(ctx: Principal, ref: str) -> Any:
    """A filter on :func:`_todos` to the board ``ref`` names, by id or key, joined in the same
    query. A reference that names no board filters to nothing, as an unknown id does."""
    key = parse_board_key(ref)
    if key is None:
        return todo.c.board_id == ref
    return and_(_in_workspace(ctx), board.c.key == key)


def _partial_updates(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Build the SET clause for a partial update: apply non-``None`` values only.

    An omitted field arrives as ``None`` and is left untouched. The public tool
    surface never passes a sentinel to clear a field, so omission is the only
    semantics callers can express.
    """
    return {k: v for k, v in candidate.items() if v is not None}


async def _upsert_returning(
    conn: AsyncConnection,
    *,
    insert_values: dict[str, Any],
    reselect_predicate: Any,
) -> tuple[RowMapping, bool]:
    """Race-safe idempotent to-do insert: ``INSERT ... ON CONFLICT DO NOTHING`` + re-select.

    The mechanism behind the load-bearing idempotent create. We never check-then-insert (two requests could both see "absent" and both
    insert, double-writing under ``uq_todo_idem``). Instead:

      1. ``INSERT ... ON CONFLICT (uq_todo_idem) DO NOTHING RETURNING *`` — at most
         one of any number of concurrent inserts wins; the losers no-op.
      2. If the insert returned a row, we created it. If it returned nothing, a
         concurrent (or prior) insert already holds the dedupe key, so we re-select
         by the same key and return that row.

    Either way the caller converges to exactly one row, never a duplicate, never an
    ``IntegrityError`` surfacing as a 500. The conflict target is the partial unique
    index over ``(tenant_id, workspace_id, board_id, idempotency_key)``; the
    ``index_where`` mirrors its ``postgresql_where`` so only a populated key dedupes.
    """
    stmt = (
        insert_for(conn, todo)
        .values(**insert_values)
        .on_conflict_do_nothing(
            index_elements=[
                todo.c.tenant_id,
                todo.c.workspace_id,
                todo.c.board_id,
                todo.c.idempotency_key,
            ],
            index_where=todo.c.idempotency_key.isnot(None),
        )
        .returning(todo)
    )
    inserted = (await conn.execute(stmt)).mappings().first()
    if inserted is not None:
        return inserted, True  # we created the row
    # Lost the race (or a prior call already inserted): re-select the winner.
    existing = await _one(conn, _todos().where(reselect_predicate))
    if existing is None:  # pragma: no cover - only if the row vanished mid-txn
        raise RuntimeError("todo upsert conflict but the conflicting row is not visible")
    return existing, False  # matched an existing row


# --------------------------------------------------------------------------- #
# Default-board resolution                     #
# --------------------------------------------------------------------------- #


async def _resolve_default_board(conn: AsyncConnection, ctx: Principal) -> RowMapping:
    """Return the workspace's default board, auto-creating it on first use.

    Zero-ceremony start: ``create_todo`` / ``plan_todos`` with no ``board_id`` land
    on this board, which is created on first use. The default board is the one row
    with ``is_default = true`` in scope, kept unique by the partial unique index
    ``uq_board_default`` (exactly one default per workspace).

    Race-safe like the to-do upsert: a concurrent first-use from two requests would
    both see "no default" and both try to insert, colliding on ``uq_board_default``.
    So we ``INSERT ... ON CONFLICT DO NOTHING`` the default board, then re-select
    the (now-guaranteed-present) default — the loser converges on the winner's row
    rather than raising. The board is born with the four canonical lanes
    (:data:`_DEFAULT_COLUMNS`).
    """
    existing = await _one(conn, select(board).where(board.c.is_default.is_(True)))
    if existing is not None:
        return existing

    now = _now()
    stmt = (
        insert_for(conn, board)
        .values(
            id=new_board_id(),
            **ctx.identity_columns(),
            key=DEFAULT_BOARD_KEY,
            name=_DEFAULT_BOARD_NAME,
            description=None,
            columns=_DEFAULT_COLUMNS,
            is_default=True,
            created_at=now,
            updated_at=now,
        )
        # No conflict target: a concurrent first use collides on uq_board_default, and on
        # uq_board_key too (both carry TODO), and either one means the default exists.
        .on_conflict_do_nothing()
    )
    await conn.execute(stmt)
    # Whether we won or lost the race, exactly one default board now exists; re-select it.
    row = await _one(conn, select(board).where(board.c.is_default.is_(True)))
    if row is None:  # pragma: no cover - the index guarantees one exists post-insert
        raise RuntimeError("default board insert succeeded but no default board is visible")
    return row


async def _resolve_board(conn: AsyncConnection, ctx: Principal, board_id: str | None) -> RowMapping:
    """Resolve a target board: the given ``board_id``, an id or a key (404 if out of scope),
    else the default.

    A given ``board_id`` is validated to be in scope and returned; ``None`` resolves
    (and may auto-create) the workspace default board.

    The board is resolved within what the caller may read, which is its own workspace. A
    runtime that granted reads across workspaces would also have to constrain a write target
    to ``workspace_id == ctx.workspace_id``, so a to-do (stamped to the caller's workspace)
    could not be attached to a board in a merely read-granted workspace.
    """
    if board_id is not None:
        return await _board_by_ref(conn, ctx, board_id)
    return await _resolve_default_board(conn, ctx)


async def _next_sort_order(conn: AsyncConnection, board_id: str, status: str) -> int:
    """Append position: one past the max ``sort_order`` in this board's status lane.

    A new to-do appends at the END of its lane. ``sort_order`` is an integer
    reordered in batches (``reorder_todos``), not a float-rebalance scheme. Returns 0
    for an empty lane.
    """
    result = await conn.execute(
        select(func.coalesce(func.max(todo.c.sort_order), -1)).where(
            and_(todo.c.board_id == board_id, todo.c.status == status)
        )
    )
    return int(result.scalar_one()) + 1


async def _lane_next_orders(conn: AsyncConnection, board_id: str) -> dict[str, int]:
    """Per-status next ``sort_order`` for a board, read in ONE query.

    ``plan_todos`` appends many to-dos across (possibly several) status lanes; calling
    :func:`_next_sort_order` per item would be N ``SELECT max`` round-trips. Instead read
    the max per lane once and increment in memory. Returns ``{status: max+1}``; a lane
    absent from the map starts at 0.
    """
    rows = (
        await conn.execute(
            select(todo.c.status, func.max(todo.c.sort_order))
            .where(todo.c.board_id == board_id)
            .group_by(todo.c.status)
        )
    ).all()
    return {r[0]: int(r[1]) + 1 for r in rows}


def _parse_plan_item(item: Any, index: int) -> PlanItem:
    """Coerce + validate one plan step (a dict over MCP, or a PlanItem) into PlanItem.

    Typed coercion fixes the per-item ``due_at`` (a JSON string → datetime) and rejects a
    bad field (a non-string status, an unknown key) with a clean error instead of
    silently dropping it. Pydantic's ``ValidationError`` is a ``ValueError``; we wrap it
    with a ``todos[i]`` prefix so the agent learns WHICH step is wrong.
    """
    try:
        return PlanItem.model_validate(item)
    except ValidationError as exc:
        raise ValueError(f"todos[{index}] is invalid: {exc}") from exc


# --------------------------------------------------------------------------- #
# Create / plan                                                                #
# --------------------------------------------------------------------------- #


async def create_todo(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    title: str,
    details: str | None = None,
    board_id: str | None = None,
    parent_id: str | None = None,
    priority: str | None = None,
    labels: list[str] | None = None,
    assignee: str | None = None,
    due_at: datetime | None = None,
    status: str | None = None,
    blocked_reason: str | None = None,
    idempotency_key: str | None = None,
    source: str | None = None,
) -> tuple[Todo, str]:
    """Create a to-do on a board, auto-creating the workspace default board if needed.

    Zero-ceremony start: omit ``board_id`` and the to-do lands on the
    workspace default board, created on first use. ``status`` defaults to ``open``,
    ``priority`` to ``normal``; ``sort_order`` appends at the end of the to-do's lane. A
    to-do created ``done``/``cancelled`` carries ``completed_at``, and one created
    ``blocked`` needs ``blocked_reason`` (:func:`_create_side_effects`).
    A given ``parent_id`` (an id or a key) is validated to be in scope (404 otherwise).
    The to-do takes the board's next number.

    Idempotency: with an ``idempotency_key``, a retry on
    the SAME board converges to one row under ``uq_todo_idem`` via ON CONFLICT (see
    :func:`_upsert_returning`); a concurrent double-create resolves to a single row, and
    the replay takes no number.
    Returns ``(Todo, action)`` with ``action`` ∈ {created, matched}.
    """
    _check_len("title", title, _CAP_TITLE)
    _check_len("details", details, _CAP_DETAILS)
    _check_len("priority", priority, _CAP_SHORT)
    _check_len("assignee", assignee, _CAP_SHORT)
    _check_len("status", status, _CAP_SHORT)
    _check_len("idempotency_key", idempotency_key, _CAP_KEY)
    _check_len("source", source, _CAP_SHORT)
    title = title.strip()
    if not title:
        raise ValueError("title must not be empty")
    resolved_status = _normalize_status(status)
    now = _now()
    side_effects = _create_side_effects(resolved_status, blocked_reason, now)
    clean_labels = _clean_labels(labels)

    board_row = await _resolve_board(conn, ctx, board_id)
    target_board_id = board_row["id"]

    if parent_id is not None:
        parent_id = (await _resolve_todo(conn, ctx, parent_id))["id"]  # 404 if out of scope

    number = await _claim_number(conn, target_board_id)
    insert_values: dict[str, Any] = {
        "id": new_todo_id(),
        **ctx.identity_columns(),
        "board_id": target_board_id,
        "parent_id": parent_id,
        "number": number,
        "title": title,
        "details": _clean(details),
        "status": resolved_status,
        "priority": _clean(priority) or _DEFAULT_PRIORITY,
        "labels": clean_labels,
        "assignee": _clean(assignee),
        "sort_order": await _next_sort_order(conn, target_board_id, resolved_status),
        "due_at": _ensure_utc(due_at),
        **side_effects,
        "archived_at": None,
        "idempotency_key": _clean(idempotency_key),
        "source": _clean(source),
        "created_at": now,
        "updated_at": now,
    }

    if insert_values["idempotency_key"] is not None:
        # Retry-safe path: a second create with the same key on the same board
        # converges to the row the first call inserted — no duplicate.
        row, created = await _upsert_returning(
            conn,
            insert_values=insert_values,
            reselect_predicate=and_(
                todo.c.tenant_id == ctx.tenant_id,
                todo.c.workspace_id == ctx.workspace_id,
                todo.c.board_id == target_board_id,
                todo.c.idempotency_key == insert_values["idempotency_key"],
            ),
        )
    else:
        # No key → plain create. RETURNING avoids a post-insert re-select (symmetric with
        # plan_todos).
        inserted = (
            (await conn.execute(todo.insert().values(**insert_values).returning(todo)))
            .mappings()
            .first()
        )
        if inserted is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
            raise RuntimeError("todo insert returned no row")
        row, created = inserted, True
    if created:
        await _advance_number(conn, target_board_id, number + 1)

    action = "created" if created else "matched"
    # A matched create changed no row — reflect it in the audit detail so the change log
    # distinguishes a real create from an idempotent no-op convergence. A real create names
    # the side-effect columns its starting status set.
    await write_audit(
        conn,
        ctx,
        todo_id=row["id"],
        action="create",
        outcome="ok",
        detail=_set_fields(side_effects) if created else {"matched": True},
    )
    return Todo.from_row(row, board_row["key"]), action


async def plan_todos(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    todos: list[Any],
    board_id: str | None = None,
    parent_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[list[Todo], int]:
    """Lay down a multi-step plan in one additive call — the decomposition primitive.

    ADDITIVE, never whole-list-replace, so it is safe on a board a
    human also edits. An optional shared ``parent_id`` (validated once, in scope)
    groups every step under one goal. Each item is created as a to-do on the resolved
    board (the workspace default if none given). Per-item idempotency derives from the
    call key + index (``f"{key}:{i}"``), so a RETRIED plan converges to the same rows
    instead of duplicating. New to-dos take the board's next numbers in plan order; a
    step that converges on an existing row keeps its number and takes none.

    Each item (a dict over MCP, or a PlanItem) is coerced + validated via
    :func:`_parse_plan_item` — a string ``due_at`` becomes a datetime, a non-string field
    or unknown key fails as a clean ``ValueError`` → ``invalid_request`` instead of being
    silently dropped. The item count is capped (``_CAP_PLAN_ITEMS``) and the per-lane
    ``sort_order`` is read ONCE (not per item), and a plain insert uses ``RETURNING`` (no
    post-insert re-select), so a large plan stays bounded — no per-item N+1.

    Returns ``(todos, created_count)`` — the created/matched To-do wire models and the
    number that landed as NEW (vs. matched on retry).
    """
    if not isinstance(todos, list):
        raise TypeError("todos must be a list of step dicts")
    if len(todos) > _CAP_PLAN_ITEMS:
        raise ValueError(f"a plan may have at most {_CAP_PLAN_ITEMS} items (got {len(todos)})")

    # Coerce + validate every item up front (uniform typed handling — fixes the due_at
    # string drop and a non-string status at once; rejects a bad item cleanly).
    items = [_parse_plan_item(item, i) for i, item in enumerate(todos)]
    if not items:
        # An empty plan is a no-op — don't auto-create a default board or write an audit
        # row for a plan that creates nothing.
        return [], 0

    board_row = await _resolve_board(conn, ctx, board_id)
    target_board_id = board_row["id"]

    if parent_id is not None:
        # Validate the shared parent once (an id or a key); 404 otherwise.
        parent_id = (await _resolve_todo(conn, ctx, parent_id))["id"]

    call_key = _clean(idempotency_key)
    # Per-lane next sort_order read ONCE, then incremented in memory (no per-item SELECT).
    lane_next = await _lane_next_orders(conn, target_board_id)
    # The board's next number, claimed once and advanced in memory as steps land.
    first_number = next_number = await _claim_number(conn, target_board_id)

    created_count = 0
    out: list[Todo] = []
    now = _now()

    for i, item in enumerate(items):
        title = item.title.strip()
        if not title:
            raise ValueError(f"todos[{i}].title is required and must be a non-empty string")
        _check_len(f"todos[{i}].title", title, _CAP_TITLE)
        _check_len(f"todos[{i}].details", item.details, _CAP_DETAILS)
        _check_len(f"todos[{i}].priority", item.priority, _CAP_SHORT)
        _check_len(f"todos[{i}].status", item.status, _CAP_SHORT)
        resolved_status = _normalize_status(item.status)
        side_effects = _create_side_effects(
            resolved_status, item.blocked_reason, now, field=f"todos[{i}].blocked_reason"
        )

        # Append position for this lane, incremented in memory (no per-item SELECT max).
        order = lane_next.get(resolved_status, 0)
        lane_next[resolved_status] = order + 1

        # Per-item idempotency key: call key + index so a retried plan converges
        # step-for-step. Absent a call key, each item is a plain create.
        item_key = f"{call_key}:{i}" if call_key is not None else None

        insert_values: dict[str, Any] = {
            "id": new_todo_id(),
            **ctx.identity_columns(),
            "board_id": target_board_id,
            "parent_id": parent_id,
            "number": next_number,
            "title": title,
            "details": _clean(item.details),
            "status": resolved_status,
            "priority": _clean(item.priority) or _DEFAULT_PRIORITY,
            "labels": _clean_labels(item.labels),
            "assignee": None,
            "sort_order": order,
            "due_at": _ensure_utc(item.due_at),
            **side_effects,
            "archived_at": None,
            "idempotency_key": item_key,
            "source": None,
            "created_at": now,
            "updated_at": now,
        }

        if item_key is not None:
            row, created = await _upsert_returning(
                conn,
                insert_values=insert_values,
                reselect_predicate=and_(
                    todo.c.tenant_id == ctx.tenant_id,
                    todo.c.workspace_id == ctx.workspace_id,
                    todo.c.board_id == target_board_id,
                    todo.c.idempotency_key == item_key,
                ),
            )
        else:
            # RETURNING avoids the post-insert re-select (the avoidable per-item N+1).
            inserted = (
                (await conn.execute(todo.insert().values(**insert_values).returning(todo)))
                .mappings()
                .first()
            )
            if inserted is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
                raise RuntimeError("todo insert returned no row")
            row, created = inserted, True

        if created:
            created_count += 1
            next_number += 1
        out.append(Todo.from_row(row, board_row["key"]))

    if next_number != first_number:
        await _advance_number(conn, target_board_id, next_number)

    # One audit row for the plan call, carrying counts only (no PII): the number of
    # items in the plan and how many were newly created.
    await write_audit(
        conn,
        ctx,
        todo_id=parent_id or target_board_id,
        action="plan",
        outcome="ok",
        detail={"items": len(items), "created": created_count},
    )
    return out, created_count


# --------------------------------------------------------------------------- #
# Fields                                                                        #
# --------------------------------------------------------------------------- #


async def update_todo(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    todo_id: str,
    title: str | None = None,
    details: str | None = None,
    priority: str | None = None,
    labels: list[str] | None = None,
    assignee: str | None = None,
    due_at: datetime | None = None,
    parent_id: str | None = None,
) -> Todo:
    """Partial, null-safe field update. Omitted (``None``) fields are unchanged.

    Bumps ``updated_at``. The activity row names the fields set (``{"fields": [...]}``),
    never their values. **Does NOT touch ``status``** — status moves only through
    the transition verbs. 404 (not 403) if the to-do is out of scope. A new
    ``parent_id`` is validated to be in scope first (404 otherwise). Either may be a key.
    """
    _check_len("title", title, _CAP_TITLE)
    _check_len("details", details, _CAP_DETAILS)
    _check_len("priority", priority, _CAP_SHORT)
    _check_len("assignee", assignee, _CAP_SHORT)
    todo_id = (await _resolve_todo(conn, ctx, todo_id))["id"]  # 404 if out of scope
    if parent_id is not None:
        # Validate the edge target in scope (404 otherwise), then guard the trivial cycle
        # (a to-do as its own parent). Deeper cycles (X→Y→X) are not yet walked — deferred
        # until recursive subtree traversal lands.
        parent_id = (await _resolve_todo(conn, ctx, parent_id))["id"]
        if parent_id == todo_id:
            raise ValueError("a to-do cannot be its own parent")

    candidate: dict[str, Any] = {
        "title": title.strip() if isinstance(title, str) and title.strip() else None,
        "details": _clean(details),
        "priority": _clean(priority),
        "labels": _clean_labels(labels),
        "assignee": _clean(assignee),
        "due_at": _ensure_utc(due_at),
        "parent_id": parent_id,
    }
    updates = _partial_updates(candidate)
    if updates:
        updates["updated_at"] = _now()
        await conn.execute(todo.update().where(todo.c.id == todo_id).values(**updates))
    row = await _todo_row(conn, todo_id)
    # Always audit the call, as every update does — the row records the update attempt
    # even when the partial change set was empty. Its detail names the fields set.
    await write_audit(
        conn,
        ctx,
        todo_id=todo_id,
        action="update",
        outcome="ok",
        detail={"fields": [name for name in candidate if name in updates]},
    )
    return Todo.from_row(row)


# --------------------------------------------------------------------------- #
# Transitions (encode side effects; self-documenting for the model)            #
# --------------------------------------------------------------------------- #
#
# Each transition: load-or-404; set status + its owned side effect; bump
# updated_at; audit; return (To-do, previous_status). Each verb OWNS its side
# effect (timestamps, blocked_reason) so the side effect can't be forgotten
# (the agent picks the verb that names what happened). There is no generic
# set_status, on purpose.


async def _transition(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    todo_id: str,
    action: str,
    new_status: str,
    extra: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> tuple[Todo, str]:
    """Shared transition body: load-or-404, set status + side effects, audit, return.

    ``extra`` carries the verb's owned side-effect columns (e.g. ``completed_at`` /
    ``blocked_reason``); ``updated_at`` is always bumped. Returns
    ``(Todo, previous_status)`` so the tool can report what it moved FROM.
    """
    row = await _resolve_todo(conn, ctx, todo_id)  # 404 if out of scope
    todo_id = row["id"]
    previous_status = row["status"]
    values = {"status": new_status, **extra, "updated_at": _now()}
    await conn.execute(todo.update().where(todo.c.id == todo_id).values(**values))
    fresh = await _todo_row(conn, todo_id)
    await write_audit(conn, ctx, todo_id=todo_id, action=action, outcome="ok", detail=detail)
    return Todo.from_row(fresh), previous_status


async def start_todo(conn: AsyncConnection, ctx: Principal, *, todo_id: str) -> tuple[Todo, str]:
    """→ ``in_progress``; clears ``blocked_reason``. Returns ``(Todo, previous_status)``."""
    return await _transition(
        conn,
        ctx,
        todo_id=todo_id,
        action="start",
        new_status="in_progress",
        extra={"blocked_reason": None},
    )


async def complete_todo(conn: AsyncConnection, ctx: Principal, *, todo_id: str) -> tuple[Todo, str]:
    """→ ``done``; sets ``completed_at``. Returns ``(Todo, previous_status)``."""
    return await _transition(
        conn,
        ctx,
        todo_id=todo_id,
        action="complete",
        new_status="done",
        extra={"completed_at": _now()},
    )


async def block_todo(
    conn: AsyncConnection, ctx: Principal, *, todo_id: str, reason: str
) -> tuple[Todo, str]:
    """→ ``blocked``; sets ``blocked_reason``. Returns ``(Todo, previous_status)``.

    ``reason`` is required — a blocker without a reason is not actionable. It is the
    one transition-verb field stored on the to-do (``blocked_reason``), cleared again
    by ``start_todo`` / ``reopen_todo``.
    """
    _check_len("reason", reason, _CAP_REASON)
    cleaned = _clean(reason)
    if cleaned is None:
        raise ValueError("reason is required to block a todo")
    return await _transition(
        conn,
        ctx,
        todo_id=todo_id,
        action="block",
        new_status="blocked",
        extra={"blocked_reason": cleaned},
    )


async def reopen_todo(conn: AsyncConnection, ctx: Principal, *, todo_id: str) -> tuple[Todo, str]:
    """→ ``open``; clears ``completed_at`` and ``blocked_reason``. Returns ``(Todo, previous_status)``."""
    return await _transition(
        conn,
        ctx,
        todo_id=todo_id,
        action="reopen",
        new_status="open",
        extra={"completed_at": None, "blocked_reason": None},
    )


async def cancel_todo(conn: AsyncConnection, ctx: Principal, *, todo_id: str) -> tuple[Todo, str]:
    """→ ``cancelled`` ("won't do"); sets ``completed_at``. Returns ``(Todo, previous_status)``.

    A terminal status distinct from ``done``. It takes no reason: narration is user content,
    kept out of the no-PII audit trail.
    """
    return await _transition(
        conn,
        ctx,
        todo_id=todo_id,
        action="cancel",
        new_status="cancelled",
        extra={"completed_at": _now()},
    )


# --------------------------------------------------------------------------- #
# Order / cleanup                                                              #
# --------------------------------------------------------------------------- #


async def reorder_todos(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    board_id: str,
    status: str,
    todo_ids: list[str],
) -> int:
    """Batch position update within a status lane (the board UI sends the full order).

    Validates the board is in scope (404 otherwise) and that EVERY id in ``todo_ids``
    is a to-do in this (tenant, ws, board, status) lane — an id that is out of scope,
    on another board, or in another lane raises ``TodoNotFoundError`` (404). The board and
    each entry may be an id or a key. Then sets each to-do's ``sort_order`` to its index in the list.
    Returns the count reordered.

    ``sort_order`` is an integer reordered in batches, not a float-rebalance
    scheme — the UI sends the full lane order on drop.
    """
    if not isinstance(todo_ids, list):
        raise TypeError("todo_ids must be a list of to-do ids")
    # Keys resolve to ids first, in one query, so a to-do named both ways counts as a
    # duplicate. The reference as given is what a not-found reports.
    parsed = {ref: parse_todo_key(ref) for ref in todo_ids}
    pairs = {p for p in parsed.values() if p is not None}
    by_key: dict[tuple[str, int], str] = {}
    if pairs:
        rows = await _all(
            conn,
            select(board.c.key, todo.c.number, todo.c.id)
            .select_from(todo.join(board, todo.c.board_id == board.c.id))
            .where(
                _in_workspace(ctx),
                tuple_(board.c.key, todo.c.number).in_(sorted(pairs)),
            ),
        )
        by_key = {(r["key"], r["number"]): r["id"] for r in rows}
    given: dict[str, str] = {}
    resolved: list[str] = []
    for ref in todo_ids:
        pair = parsed[ref]
        if pair is None:
            tid = ref
        elif pair in by_key:
            tid = by_key[pair]
        else:
            raise TodoNotFoundError(ref)
        given.setdefault(tid, ref)
        resolved.append(tid)
    todo_ids = resolved
    if len(set(todo_ids)) != len(todo_ids):
        # A duplicate id would pass the membership check but make the position
        # assignment + reordered count ambiguous; reject it explicitly.
        raise ValueError("todo_ids must not contain duplicate ids")
    _check_len("status", status, _CAP_SHORT)
    board_id = (await _board_by_ref(conn, ctx, board_id))["id"]  # 404 if out of scope

    now = _now()
    if not todo_ids:
        await write_audit(
            conn,
            ctx,
            todo_id=board_id,
            action="reorder",
            outcome="ok",
            detail={"board_id": board_id, "status": status, "count": 0},
        )
        return 0

    # 1. Membership in ONE query. Every id must be a to-do in this (tenant, ws via RLS,
    #    board, status) lane; a missing id (out of scope, other board, or other lane)
    #    raises 404 — preserving the no-silent-cross-lane-move guarantee.
    found = {
        r[0]
        for r in (
            await conn.execute(
                select(todo.c.id).where(
                    and_(
                        todo.c.board_id == board_id,
                        todo.c.status == status,
                        todo.c.id.in_(todo_ids),
                    )
                )
            )
        ).all()
    }
    missing = [tid for tid in todo_ids if tid not in found]
    if missing:
        raise TodoNotFoundError(given[missing[0]])

    # 2. Apply positions in ONE bulk UPDATE (CASE id → its list index). The board UI
    #    sends the full lane on every drop, so this turns a per-id N+1 (2N round-trips)
    #    into 2 — the read above + this write.
    position = case(
        *[(todo.c.id == tid, index) for index, tid in enumerate(todo_ids)],
        else_=todo.c.sort_order,
    )
    await conn.execute(
        todo.update()
        .where(
            and_(
                todo.c.board_id == board_id,
                todo.c.status == status,
                todo.c.id.in_(todo_ids),
            )
        )
        .values(sort_order=position, updated_at=now)
    )

    await write_audit(
        conn,
        ctx,
        todo_id=board_id,
        action="reorder",
        outcome="ok",
        detail={"board_id": board_id, "status": status, "count": len(todo_ids)},
    )
    return len(todo_ids)


async def archive_todo(conn: AsyncConnection, ctx: Principal, *, todo_id: str) -> Todo:
    """Set ``archived_at`` (hides from default views; status unchanged). 404 if out of scope.

    A soft-hide: the row stays present (its activity timeline + step edges are
    preserved; there is no ``delete_*``). ``status`` is deliberately untouched.
    """
    todo_id = (await _resolve_todo(conn, ctx, todo_id))["id"]  # 404 if out of scope
    now = _now()
    await conn.execute(
        todo.update().where(todo.c.id == todo_id).values(archived_at=now, updated_at=now)
    )
    row = await _todo_row(conn, todo_id)
    await write_audit(conn, ctx, todo_id=todo_id, action="archive", outcome="ok")
    return Todo.from_row(row)


# --------------------------------------------------------------------------- #
# Reads                                                                        #
# --------------------------------------------------------------------------- #
#
# Every read is side-effect free: it runs under the caller's RLS-scoped session,
# returns wire models (never raw rows), and emits a structured ``todos_access``
# log line via :func:`_log_access` — it NEVER writes a todo_activity row and NEVER
# auto-creates a board. Denied/error attempts are audited from the server layer.


async def list_todos(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    board_id: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    label: str | None = None,
    parent_id: str | None = None,
    include_archived: bool = False,
    query: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Todo], str | None]:
    """List to-dos with opaque keyset pagination: newest first, or best match first for a query.

    Filters are AND-combined: ``board_id`` / ``status`` / ``assignee`` /
    ``parent_id`` (each an equality), and ``label`` (the value must be present in
    the to-do's ``labels`` JSONB array — a containment predicate, NOT a per-row
    scan). ``include_archived=False`` (the default) restricts to ``archived_at IS
    NULL`` so soft-hidden todos drop out of the default view; pass ``True`` to see
    them.

    ``query`` is split on whitespace into tokens; a to-do matches when EVERY token is a
    case-insensitive substring (``ILIKE``) of ``title`` OR ``details``, with ``%``/``_``
    in a token matched literally. A query with no tokens filters nothing. With tokens the
    order is the match tier (:func:`_match_tier`), then newest first, and the cursor names
    a position in that order (the ``todos_search`` listing): a plain listing's cursor is
    refused there, and a query listing's cursor is refused without a query.

    Returns ``(todos, next_cursor)`` — ``next_cursor`` is an opaque token (encodes
    the last row's position) returned ONLY when a full page came back, else ``None``
    at the end. ``limit`` is clamped (default 50, max 200). One query; RLS scopes the
    whole thing to the caller's (tenant, workspace).
    """
    limit = _clamp_limit(limit)
    q = (query or "").strip()
    tokens = q.split()
    listing = _TODOS
    stmt = _todos()
    if tokens:
        listing = _search_listing(q)
        stmt = stmt.add_columns(listing.order[0].expr).where(
            _token_predicate(tokens, (todo.c.title, todo.c.details))
        )
    if board_id is not None:
        stmt = stmt.where(_on_board(ctx, board_id))
    if status is not None:
        stmt = stmt.where(todo.c.status == status)
    if assignee is not None:
        stmt = stmt.where(todo.c.assignee == assignee)
    if parent_id is not None:
        # A key names the parent as well as its id does; one that names nothing filters to
        # nothing, as an unknown id does.
        parent = await _key_lookup(conn, ctx, parent_id)
        stmt = stmt.where(todo.c.parent_id == (parent["id"] if parent is not None else parent_id))
    if label is not None:
        # Containment on the JSON array, in the database — no per-row Python scan.
        stmt = stmt.where(json_array_contains(conn, todo.c.labels, label))
    if not include_archived:
        stmt = stmt.where(todo.c.archived_at.is_(None))
    stmt = listing.apply(stmt, cursor)
    rows = await _all(conn, stmt.limit(limit + 1))
    page, next_cursor = listing.paginate(rows, limit)
    _log_access(ctx, action="search" if tokens else "list")
    return [Todo.from_row(r) for r in page], next_cursor


async def get_todo(
    conn: AsyncConnection, ctx: Principal, *, todo_id: str
) -> tuple[Todo, list[Todo], dict[str, int], list[TodoActivity]]:
    """Composite read: a to-do + its direct steps + counts + recent activity timeline.

    Load-or-404 (the id is out of scope or absent — no existence oracle). ``steps``
    are the DIRECT children (``parent_id == todo_id``), ordered by lane position then
    creation. ``counts`` reports ``{"steps": N, "steps_done": M}``. ``recent_activity``
    is the to-do's most recent :data:`todo_activity` rows, reverse-chronological, capped
    at 20. Bounded queries (to-do + children + activity counts + activity rows) — no N+1.
    """
    row = await _resolve_todo(conn, ctx, todo_id)  # 404 if out of scope; an id or a key
    todo_id = row["id"]

    step_rows = await _all(
        conn,
        _todos()
        .where(todo.c.parent_id == todo_id)
        .order_by(todo.c.sort_order.asc(), todo.c.created_at.asc(), todo.c.id.asc()),
    )
    steps = [Todo.from_row(r) for r in step_rows]
    steps_done = sum(1 for s in steps if s.status in _TERMINAL_STATUSES)

    activity_rows = await _all(
        conn,
        select(todo_activity)
        .where(todo_activity.c.todo_id == todo_id)
        .order_by(todo_activity.c.at.desc(), todo_activity.c.id.desc())
        .limit(_RECENT_ACTIVITY_LIMIT),
    )

    _log_access(ctx, action="get", todo_id=todo_id)
    return (
        Todo.from_row(row),
        steps,
        {"steps": len(steps), "steps_done": steps_done},
        [TodoActivity.from_row(r) for r in activity_rows],
    )


async def get_next_todo(
    conn: AsyncConnection, ctx: Principal, *, board_id: str | None = None
) -> Todo | None:
    """The single highest-priority ACTIONABLE to-do, or ``None`` if there is none.

    Actionable = ``status = 'open'`` AND ``archived_at IS NULL`` (so a blocked,
    in-progress, done, cancelled, or archived to-do is never returned), optionally
    restricted to one ``board_id``. Ordered by priority rank (``urgent`` > ``high`` >
    ``normal`` > ``low``, any unknown priority last — via a SQL ``CASE``), then
    ``sort_order ASC, created_at ASC`` so ties resolve to the agent's own lane order.
    ``LIMIT 1`` — one query, no list scan.

    This is the agent's "what do I do next" so it need not re-derive the next item from
    ``list_todos`` each loop. Read-only: it does NOT resolve-or-create a default
    board — with no ``board_id`` it considers every actionable to-do in scope.
    """
    stmt = _todos().where(and_(todo.c.status == _DEFAULT_STATUS, todo.c.archived_at.is_(None)))
    if board_id is not None:
        stmt = stmt.where(_on_board(ctx, board_id))
    stmt = stmt.order_by(
        _priority_rank_case().asc(),
        todo.c.sort_order.asc(),
        todo.c.created_at.asc(),
        todo.c.id.asc(),
    ).limit(1)
    row = await _one(conn, stmt)
    _log_access(ctx, action="next", todo_id=row["id"] if row is not None else None)
    return Todo.from_row(row) if row is not None else None


async def get_board_summary(
    conn: AsyncConnection, ctx: Principal, *, board_id: str | None = None
) -> tuple[Board, list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """Read-only board health: per-lane counts + WIP, totals, and what needs attention.

    Resolves the board READ-ONLY: a given ``board_id`` (an id or a key) loads via
    :func:`_board_by_ref` (404 if out of scope); ``None`` resolves the workspace default board via an
    ``is_default`` SELECT and raises :class:`BoardNotFoundError` if none exists —
    a read NEVER auto-creates a board (side-effect free; contrast
    :func:`_resolve_default_board`, the WRITE path).

    ``lanes`` is one entry per board column (``key`` / ``label`` / ``status`` +
    ``count`` of ACTIVE — non-archived — to-dos in that status, plus the column's
    ``wip_limit`` and a ``wip_ok`` flag when a limit is set). ``totals`` reports
    ``active`` / ``overdue`` / ``stalled`` / ``blocked``. ``attention`` repeats the
    actionable subset (``overdue`` / ``stalled`` / ``blocked``) for the agent's
    self-check. Every count is a single ``GROUP BY`` / filtered aggregate — no N+1,
    no per-lane loop of round-trips.
    """
    board_row = await _board_by_ref(conn, ctx, board_id) if board_id is not None else None
    if board_row is None:
        board_row = await _one(conn, select(board).where(board.c.is_default.is_(True)))
        if board_row is None:
            # A read must be side-effect free: with no default board we 404 rather
            # than auto-create one (that is the WRITE path, _resolve_default_board).
            raise BoardNotFoundError("default")
    resolved_board_id = board_row["id"]
    now = _now()
    stalled_before = now - timedelta(days=_STALLED_AFTER_DAYS)
    board_scope = todo.c.board_id == resolved_board_id
    active = and_(board_scope, todo.c.archived_at.is_(None))

    # Per-status ACTIVE counts in ONE aggregate (NOT a per-lane query loop).
    status_counts = {
        r[0]: int(r[1])
        for r in (
            await conn.execute(
                select(todo.c.status, func.count()).where(active).group_by(todo.c.status)
            )
        ).all()
    }

    # Attention counts, each a single filtered aggregate over the active set.
    overdue = await _count(
        conn,
        and_(
            active,
            todo.c.due_at.isnot(None),
            todo.c.due_at < now,
            todo.c.status.notin_(_TERMINAL_STATUSES),
        ),
    )
    stalled = await _count(
        conn,
        and_(active, todo.c.status == "in_progress", todo.c.updated_at < stalled_before),
    )
    blocked = status_counts.get("blocked", 0)
    # "active" = genuinely active work: non-archived AND non-terminal (done/cancelled are
    # excluded). The per-lane counts above still show the terminal lanes.
    active_total = sum(c for s, c in status_counts.items() if s not in _TERMINAL_STATUSES)

    columns = board_row["columns"] or []
    lanes: list[dict[str, Any]] = []
    for col in columns:
        col_status = col.get("status")
        count = status_counts.get(col_status, 0)
        lane: dict[str, Any] = {
            "key": col.get("key"),
            "label": col.get("label"),
            "status": col_status,
            "count": count,
        }
        wip_limit = col.get("wip_limit")
        if wip_limit is not None:
            lane["wip_limit"] = wip_limit
            # Advisory only: a breach is
            # surfaced, never blocked.
            lane["wip_ok"] = count <= wip_limit
        lanes.append(lane)

    _log_access(ctx, action="summary", todo_id=resolved_board_id)
    return (
        Board.from_row(board_row),
        lanes,
        {"active": active_total, "overdue": overdue, "stalled": stalled, "blocked": blocked},
        {"overdue": overdue, "stalled": stalled, "blocked": blocked},
    )


# --------------------------------------------------------------------------- #
# Boards                                                                        #
# --------------------------------------------------------------------------- #


async def list_boards(conn: AsyncConnection, ctx: Principal) -> list[Board]:
    """List the workspace's boards, newest first. RLS scopes to the caller's (tenant, ws).

    A read: emits a structured access log, no table row.
    """
    rows = await _all(
        conn,
        select(board).order_by(board.c.created_at.desc(), board.c.id.desc()),
    )
    _log_access(ctx, action="list_boards")
    return [Board.from_row(r) for r in rows]


async def create_board(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    name: str,
    description: str | None = None,
    columns: list[dict[str, Any]] | None = None,
    key: str | None = None,
) -> Board:
    """Create a (non-default) board; ``columns`` defaults to the four canonical lanes.

    Minted ``bd_<ulid>``; ``is_default`` is always ``False`` — the single default
    board is auto-created on first ``create_todo``, never via this tool, so
    the ``uq_board_default`` invariant is never at risk here. Omitting ``columns``
    gives the board the four canonical lanes (:data:`_DEFAULT_COLUMNS`).

    ``key`` is fixed here for the board's life. Given, it must follow the key rule and be
    free in the workspace; omitted, it is derived from ``name``
    (:func:`mcp_todos.keys.derive_board_key`). A taken key, or a name whose derived key
    has no free suffix left, is a ``ValueError`` (→ ``invalid_request``). ``TODO`` is the
    default board's.
    """
    _check_len("name", name, _CAP_BOARD_NAME)
    _check_len("description", description, _CAP_DESCRIPTION)
    _check_len("key", key, _CAP_SHORT)
    name = name.strip()
    if not name:
        raise ValueError("name must not be empty")
    explicit = normalize_board_key(key) if key is not None else None
    if explicit == DEFAULT_BOARD_KEY:
        raise ValueError(f"key {DEFAULT_BOARD_KEY} is reserved for the workspace default board")

    board_id = new_board_id()
    # A derived key that a concurrent create takes first is derived again; an explicit one
    # that is taken is the caller's to change.
    for _attempt in range(3):
        taken = set(
            (
                await conn.execute(
                    select(board.c.key).where(
                        board.c.tenant_id == ctx.tenant_id,
                        board.c.workspace_id == ctx.workspace_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if explicit is not None:
            if explicit in taken:
                raise ValueError(
                    f"key {explicit} is already used by another board in this workspace; "
                    "choose another key"
                )
            board_key = explicit
        else:
            derived = derive_board_key(name, taken)
            if derived is None:
                raise ValueError(
                    f"every key derived from {name!r} is already used in this workspace; "
                    "pass an explicit key (2-5 of A-Z/0-9, starting with a letter)"
                )
            board_key = derived
        now = _now()
        inserted = (
            await conn.execute(
                insert_for(conn, board)
                .values(
                    id=board_id,
                    **ctx.identity_columns(),
                    key=board_key,
                    name=name,
                    description=_clean(description),
                    columns=columns if columns is not None else _DEFAULT_COLUMNS,
                    is_default=False,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[board.c.tenant_id, board.c.workspace_id, board.c.key]
                )
                .returning(board.c.id)
            )
        ).first()
        if inserted is not None:
            break
        if explicit is not None:
            raise ValueError(
                f"key {explicit} is already used by another board in this workspace; "
                "choose another key"
            )
    else:
        raise ValueError("could not reserve a board key; pass an explicit key and retry")
    row = await _board_row(conn, board_id)
    await write_audit(conn, ctx, todo_id=board_id, action="create", outcome="ok")
    return Board.from_row(row)


async def update_board(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    board_id: str,
    name: str | None = None,
    description: str | None = None,
    columns: list[dict[str, Any]] | None = None,
) -> Board:
    """Partial board update; editing ``columns`` re-maps the human view only.

    Omitted (``None``) fields are unchanged; bumps ``updated_at``. 404 (not 403) if
    the board is out of scope. Editing ``columns`` re-maps which status each lane
    renders — it never touches any to-do's canonical ``status``.
    """
    _check_len("name", name, _CAP_BOARD_NAME)
    _check_len("description", description, _CAP_DESCRIPTION)
    board_id = (await _board_by_ref(conn, ctx, board_id))["id"]  # 404 if out of scope

    candidate: dict[str, Any] = {
        "name": name.strip() if isinstance(name, str) and name.strip() else None,
        "description": _clean(description),
        "columns": columns,
    }
    updates = _partial_updates(candidate)
    if updates:
        updates["updated_at"] = _now()
        await conn.execute(board.update().where(board.c.id == board_id).values(**updates))
    row = await _board_row(conn, board_id)
    await write_audit(
        conn,
        ctx,
        todo_id=board_id,
        action="update",
        outcome="ok",
        detail={"fields": [name for name in candidate if name in updates]},
    )
    return Board.from_row(row)


# --------------------------------------------------------------------------- #
# Internal: read helpers — counting, pagination, priority rank, search          #
# --------------------------------------------------------------------------- #
#
# Keyset cursor, token search and a deterministic rank, plus the priority CASE that
# orders the to-do lanes.


async def _count(conn: AsyncConnection, predicate: Any) -> int:
    """Count ``todo`` rows matching ``predicate`` (RLS-scoped) in ONE aggregate query."""
    result = await conn.execute(select(func.count()).select_from(todo).where(predicate))
    return int(result.scalar_one())


def _clamp_limit(limit: int) -> int:
    """Clamp a page size into ``[1, _MAX_LIMIT]``; a non-positive limit → the default."""
    if limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


def _priority_rank_case() -> Any:
    """A SQL ``CASE`` mapping ``priority`` → its sort rank (lower sorts first).

    ``urgent`` (0) < ``high`` (1) < ``normal`` (2) < ``low`` (3) < any unknown
    (:data:`_PRIORITY_RANK_UNKNOWN`). Built in SQL so ``get_next_todo`` orders by priority
    in the database (``ORDER BY rank ASC``) without pulling the lane into memory —
    open-string priorities outside the known set fall through to last.
    """
    return case(
        dict(_PRIORITY_RANK),
        value=todo.c.priority,
        else_=_PRIORITY_RANK_UNKNOWN,
    )


# list_todos's total order: newest first, with the id breaking ties among rows written in one
# transaction. The name tags its cursors (pgkeyset).
_TODOS = Listing("todos", Sort.desc(todo.c.created_at), Sort.desc(todo.c.id))


# What a title word starts after, besides the title's start: a space, a tab or a line break.
_WORD_BREAKS = (" ", "\t", "\n", "\r")


def _escape_like(value: str) -> str:
    """Escape ILIKE wildcards so a literal ``%``/``_`` in the query isn't a wildcard."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _token_predicate(tokens: Sequence[str], columns: Sequence[Any]) -> Any:
    """Build the search WHERE clause: AND across tokens, OR across columns.

    Each token must be a case-insensitive substring of AT LEAST ONE of ``columns``;
    all tokens must match (AND), so a multi-word query matches across the searchable
    columns. Per-token wildcards are escaped, so
    a literal ``%``/``_`` in a token is matched literally, not as a wildcard.
    """
    return and_(
        *(
            or_(*(col.ilike(f"%{_escape_like(tok)}%", escape="\\") for col in columns))
            for tok in tokens
        )
    )


def _match_tier(query: str, tokens: Sequence[str]) -> Any:
    """How well a to-do matches ``query``, in SQL, labelled ``match_tier`` (higher sorts first).

    4 exact title > 3 the title starts with the whole query > 2 every token starts a word of
    the title (at its start, or after one of :data:`_WORD_BREAKS`) > 1 any other match (a
    token matched mid-word, or only in ``details``). Comparisons lower both sides in the
    database. The tier is computed only here, in the query, and the cursor carries the
    value the row was fetched with, so a page boundary never disagrees with the order.
    """
    title = func.lower(todo.c.title)

    def like(pattern: str) -> Any:
        return title.like(func.lower(literal(pattern)), escape="\\")

    word_starts = and_(
        *(
            or_(
                like(f"{_escape_like(tok)}%"),
                *(like(f"%{brk}{_escape_like(tok)}%") for brk in _WORD_BREAKS),
            )
            for tok in tokens
        )
    )
    tier = case(
        (title == func.lower(literal(query)), 4),
        (like(f"{_escape_like(query)}%"), 3),
        (word_starts, 2),
        else_=1,
    )
    return type_coerce(tier, Integer).label("match_tier")


def _search_listing(query: str) -> Listing:
    """The order a query's matches page in: match tier, then newest first, the id breaking
    ties. The name tags its cursors, so a plain listing's cursor is not a position here."""
    return Listing(
        "todos_search",
        Sort.desc(_match_tier(query, query.split())),
        Sort.desc(todo.c.created_at),
        Sort.desc(todo.c.id),
    )
