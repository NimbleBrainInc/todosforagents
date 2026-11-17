"""Store behavior suite — the write, board and read functions.

These tests exercise the data-access layer on a fresh SQLite file per test, through the
local runtime's session (``conftest.py``). They assert **behavior**, not implementation: default-board auto-creation and the
exactly-one-default invariant, idempotent ``create_todo`` convergence under
``uq_todo_idem``, ``plan_todos`` additive + idempotent + shared-parent behavior,
null-safe partial ``update_todo`` (+ 404), each status transition's side effects
(timestamps, ``blocked_reason``) and ``previous_status``, ``reorder_todos``
positions, ``archive_todo`` soft-hide, and board create/update/list.

The READ tools are covered too: ``list_todos`` filters (status / assignee /
label / parent / board) + ``include_archived`` + a cursor pagination round-trip;
``get_todo`` (steps + counts + recent activity, 404); ``get_next_todo`` priority
ordering (skips blocked/done/archived, None when nothing actionable, board
filter); ``get_board_summary`` (lane counts, overdue/stalled/blocked, WIP, the
no-default-board 404 with no auto-create); ``list_todos(query=...)`` (all-tokens
substring, match tiers, filters, its own cursor, no-match → empty). A final test pins the read/write
split — a read writes no ``todo_activity`` row.

Each test opens a fresh ``todos_session`` per logical request (mirroring how the
server opens one transaction per tool call), so the assertions hold across
commits — not within one accidentally-shared transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import make_ctx, session_for
from pgkeyset import InvalidCursor
from pgkeyset.testing import walk
from sqlalchemy import func, select

from mcp_todos.db import store
from mcp_todos.db.store import BoardNotFoundError, TodoNotFoundError
from mcp_todos.db.tables import board, todo, todo_activity

# ``asyncio_mode = "auto"`` (pyproject) collects every async def as an asyncio
# test, so no per-test mark is needed.


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


async def _count(ctx, table, predicate=None) -> int:
    """Count rows in ``table`` visible to ``ctx``, optional predicate."""
    stmt = select(func.count()).select_from(table)
    if predicate is not None:
        stmt = stmt.where(predicate)
    async with session_for(ctx) as conn:
        return int((await conn.execute(stmt)).scalar_one())


# --------------------------------------------------------------------------- #
# 1. Default board: auto-created on first create_todo; exactly one default     #
# --------------------------------------------------------------------------- #


async def test_create_todo_auto_creates_default_board(app_engine):
    """The first create_todo with no board_id auto-creates the workspace default
    board (name "To-dos", is_default, four canonical lanes) and lands on it."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    # No boards exist yet.
    assert await _count(ctx, board) == 0

    async with session_for(ctx) as conn:
        t, action = await store.create_todo(conn, ctx, title="First todo")
    assert action == "created"
    assert t.board_id.startswith("bd_")
    assert t.status == "open"
    assert t.priority == "normal"
    assert t.order == 0  # appended at the start of an empty lane

    # Exactly one board, and it is the default with the canonical lanes.
    async with session_for(ctx) as conn:
        boards = await store.list_boards(conn, ctx)
    assert len(boards) == 1
    default = boards[0]
    assert default.id == t.board_id
    assert default.is_default is True
    assert default.name == "To-dos"
    assert default.columns is not None
    assert [c["status"] for c in default.columns] == ["open", "in_progress", "blocked", "done"]


async def test_default_board_is_reused_and_stays_singular(app_engine):
    """Many create_todo calls (and plan_todos) reuse ONE default board; the
    exactly-one-default invariant holds (uq_board_default)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    board_ids = set()
    for i in range(4):
        async with session_for(ctx) as conn:
            t, _ = await store.create_todo(conn, ctx, title=f"Todo {i}")
        board_ids.add(t.board_id)
    async with session_for(ctx) as conn:
        planned, _ = await store.plan_todos(conn, ctx, todos=[{"title": "Plan step"}])
    board_ids.add(planned[0].board_id)

    assert len(board_ids) == 1  # all reused the single default board

    # Exactly one default board exists in scope.
    assert await _count(ctx, board, board.c.is_default.is_(True)) == 1


async def test_sort_order_appends_at_end_of_lane(app_engine):
    """Todos created on the same board/status lane append: order 0, 1, 2, …."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    orders = []
    for i in range(3):
        async with session_for(ctx) as conn:
            t, _ = await store.create_todo(conn, ctx, title=f"Todo {i}")
        orders.append(t.order)
    assert orders == [0, 1, 2]


# --------------------------------------------------------------------------- #
# 2. create_todo idempotency: same key → matched, no duplicate                 #
# --------------------------------------------------------------------------- #


async def test_create_todo_idempotent_on_key(app_engine):
    """Two create_todo with the same idempotency_key on the same board converge to
    ONE row: first created, second matched, no duplicate insert."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        t1, action1 = await store.create_todo(
            conn, ctx, title="Idempotent todo", idempotency_key="abc-123"
        )
    assert action1 == "created"

    async with session_for(ctx) as conn:
        t2, action2 = await store.create_todo(
            conn, ctx, title="Idempotent to-do (retry)", idempotency_key="abc-123"
        )
    assert action2 == "matched"
    assert t2.id == t1.id  # same row, not a duplicate

    # Exactly one to-do on the board.
    assert await _count(ctx, todo) == 1

    # The matched (no-op) create is recorded with detail {"matched": True}; the first
    # (real) create has detail None — so the change log distinguishes them.
    async with session_for(ctx) as conn:
        details = [
            r[0]
            for r in (
                await conn.execute(
                    select(todo_activity.c.detail)
                    .where(
                        (todo_activity.c.todo_id == t1.id) & (todo_activity.c.action == "create")
                    )
                    .order_by(todo_activity.c.at)
                )
            ).all()
        ]
    assert details == [None, {"matched": True}]


async def test_create_todo_no_key_always_inserts(app_engine):
    """Without an idempotency_key, repeated create_todo with the same title each
    insert a distinct row (the plain-create default)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    ids = set()
    for _ in range(3):
        async with session_for(ctx) as conn:
            t, action = await store.create_todo(conn, ctx, title="Same title")
        assert action == "created"
        ids.add(t.id)
    assert len(ids) == 3
    assert await _count(ctx, todo) == 3


async def test_create_todo_unknown_parent_raises_not_found(app_engine):
    """A create_todo with a parent_id not in scope raises TodoNotFoundError (404),
    never a silent write or integrity crash."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(TodoNotFoundError):
        async with session_for(ctx) as conn:
            await store.create_todo(conn, ctx, title="Orphan", parent_id="td_does_not_exist")


async def test_create_todo_rejects_unknown_status(app_engine):
    """A create_todo with a status outside the canonical set raises ValueError
    (→ invalid_request), keeping status the agent contract (D1)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(ValueError, match="status must be one of"):
        async with session_for(ctx) as conn:
            await store.create_todo(conn, ctx, title="Bad", status="frozen")


# --------------------------------------------------------------------------- #
# 3. plan_todos: additive, idempotent, shared parent                           #
# --------------------------------------------------------------------------- #


async def test_plan_todos_creates_all_and_is_additive(app_engine):
    """plan_todos lays down every step in one call and is ADDITIVE: a second plan
    adds more rows rather than replacing the first."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        first, created1 = await store.plan_todos(
            conn,
            ctx,
            todos=[{"title": "Step A"}, {"title": "Step B"}, {"title": "Step C"}],
        )
    assert created1 == 3
    assert [t.title for t in first] == ["Step A", "Step B", "Step C"]

    # A second plan ADDS (never clobbers the first).
    async with session_for(ctx) as conn:
        second, created2 = await store.plan_todos(conn, ctx, todos=[{"title": "Step D"}])
    assert created2 == 1
    assert await _count(ctx, todo) == 4


async def test_plan_todos_idempotent_on_call_key(app_engine):
    """A retried plan with the same call-level idempotency_key converges step-for-
    step (per-item key = f"{key}:{i}") instead of duplicating."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    items = [{"title": "Build"}, {"title": "Test"}, {"title": "Ship"}]

    async with session_for(ctx) as conn:
        first, created1 = await store.plan_todos(conn, ctx, todos=items, idempotency_key="plan-xyz")
    assert created1 == 3

    # Re-run the SAME plan with the SAME key: converges, no new rows.
    async with session_for(ctx) as conn:
        second, created2 = await store.plan_todos(
            conn, ctx, todos=items, idempotency_key="plan-xyz"
        )
    assert created2 == 0  # nothing new
    assert {t.id for t in second} == {t.id for t in first}  # same rows returned
    assert await _count(ctx, todo) == 3  # no duplicates


async def test_plan_todos_shared_parent_groups_steps(app_engine):
    """An optional shared parent_id groups every planned step under one goal
    (validated once, in scope); each step carries that parent_id."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        goal, _ = await store.create_todo(conn, ctx, title="Ship the feature")
    async with session_for(ctx) as conn:
        steps, created = await store.plan_todos(
            conn,
            ctx,
            todos=[{"title": "Design"}, {"title": "Implement"}],
            parent_id=goal.id,
        )
    assert created == 2
    assert all(s.parent_id == goal.id for s in steps)


async def test_plan_todos_unknown_parent_raises_not_found(app_engine):
    """plan_todos with a parent_id not in scope raises TodoNotFoundError (404)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(TodoNotFoundError):
        async with session_for(ctx) as conn:
            await store.plan_todos(conn, ctx, todos=[{"title": "x"}], parent_id="td_does_not_exist")


# --------------------------------------------------------------------------- #
# 3b. due_at coercion + plan input validation/caps                            #
# --------------------------------------------------------------------------- #


async def test_create_todo_persists_due_at(app_engine):
    """create_todo stores a provided due_at (the top-level typed datetime path)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    when = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Due", due_at=when)
    assert t.due_at == when


async def test_plan_todos_persists_string_due_at(app_engine):
    """A per-item due_at arriving as an ISO-8601 STRING (the MCP wire shape) is coerced
    and persisted — not silently dropped. Regression for the plan due_at data-loss bug."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        out, _ = await store.plan_todos(
            conn, ctx, todos=[{"title": "Ship", "due_at": "2026-07-01T12:00:00+00:00"}]
        )
    assert out[0].due_at == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


async def test_plan_todos_rejects_invalid_item(app_engine):
    """A malformed plan item (unparseable due_at, unknown key) raises ValueError
    (→ invalid_request) — never a silent drop or an opaque 500."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(ValueError):
        async with session_for(ctx) as conn:
            await store.plan_todos(conn, ctx, todos=[{"title": "X", "due_at": "not-a-date"}])
    with pytest.raises(ValueError):
        async with session_for(ctx) as conn:
            await store.plan_todos(conn, ctx, todos=[{"title": "X", "bogus_field": 1}])


async def test_plan_todos_caps_item_count(app_engine):
    """plan_todos rejects an over-cap plan up front (bounds the per-item insert loop)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    too_many = [{"title": f"S{i}"} for i in range(store._CAP_PLAN_ITEMS + 1)]
    with pytest.raises(ValueError, match="at most"):
        async with session_for(ctx) as conn:
            await store.plan_todos(conn, ctx, todos=too_many)


async def test_plan_todos_empty_is_noop(app_engine):
    """plan_todos([]) creates nothing: no to-dos, no auto-created default board, no audit."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        out, created = await store.plan_todos(conn, ctx, todos=[])
    assert out == []
    assert created == 0
    assert await _count(ctx, board) == 0  # no default board auto-created
    assert await _count(ctx, todo) == 0
    assert await _count(ctx, todo_activity, todo_activity.c.action == "plan") == 0


# --------------------------------------------------------------------------- #
# 4. update_todo: partial, null-safe, never status, 404                        #
# --------------------------------------------------------------------------- #


async def test_update_todo_is_null_safe_and_never_touches_status(app_engine):
    """A partial update touches only the provided fields; omitted fields are
    unchanged; status is NEVER changed by update_todo; updated_at is bumped."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(
            conn, ctx, title="Original", details="orig details", priority="low"
        )
    before_updated_at = t.updated_at
    assert t.status == "open"

    async with session_for(ctx) as conn:
        updated = await store.update_todo(
            conn, ctx, todo_id=t.id, title="Renamed", assignee="agent"
        )
    assert updated.title == "Renamed"  # provided → updated
    assert updated.assignee == "agent"  # provided → updated
    assert updated.details == "orig details"  # omitted → unchanged
    assert updated.priority == "low"  # omitted → unchanged
    assert updated.status == "open"  # update_todo never moves status
    assert updated.updated_at >= before_updated_at


async def test_update_todo_unknown_raises_not_found(app_engine):
    """update_todo on an id not in scope raises TodoNotFoundError (404, not 403)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(TodoNotFoundError):
        async with session_for(ctx) as conn:
            await store.update_todo(conn, ctx, todo_id="td_does_not_exist", title="x")


async def test_update_todo_validates_parent_id(app_engine):
    """A new parent_id is validated to be in-scope first (404 otherwise)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Child")
    with pytest.raises(TodoNotFoundError):
        async with session_for(ctx) as conn:
            await store.update_todo(conn, ctx, todo_id=t.id, parent_id="td_nope")


async def test_update_todo_rejects_self_parent(app_engine):
    """update_todo(todo_id=X, parent_id=X) is rejected — a to-do cannot be its own parent."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Self")
    with pytest.raises(ValueError, match="own parent"):
        async with session_for(ctx) as conn:
            await store.update_todo(conn, ctx, todo_id=t.id, parent_id=t.id)


# --------------------------------------------------------------------------- #
# 5. Transitions: status + side effects + previous_status                      #
# --------------------------------------------------------------------------- #


async def test_start_todo_sets_in_progress_and_clears_blocked_reason(app_engine):
    """start_todo → in_progress and clears blocked_reason; returns previous_status."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Work")
    # Block it first so start clears the reason.
    async with session_for(ctx) as conn:
        await store.block_todo(conn, ctx, todo_id=t.id, reason="waiting")

    async with session_for(ctx) as conn:
        started, previous = await store.start_todo(conn, ctx, todo_id=t.id)
    assert started.status == "in_progress"
    assert started.blocked_reason is None  # cleared
    assert previous == "blocked"


async def test_complete_todo_sets_done_and_completed_at(app_engine):
    """complete_todo → done and stamps completed_at; previous_status reported."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Finish me")
    async with session_for(ctx) as conn:
        await store.start_todo(conn, ctx, todo_id=t.id)

    async with session_for(ctx) as conn:
        done, previous = await store.complete_todo(conn, ctx, todo_id=t.id)
    assert done.status == "done"
    assert done.completed_at is not None
    assert previous == "in_progress"


async def test_block_todo_sets_blocked_and_reason(app_engine):
    """block_todo → blocked and records blocked_reason; previous_status reported."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Blocked work")

    async with session_for(ctx) as conn:
        blocked, previous = await store.block_todo(conn, ctx, todo_id=t.id, reason="needs API key")
    assert blocked.status == "blocked"
    assert blocked.blocked_reason == "needs API key"
    assert previous == "open"


async def test_block_todo_requires_reason(app_engine):
    """block_todo with an empty reason raises ValueError (a blocker needs a why)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="x")
    with pytest.raises(ValueError, match="reason is required"):
        async with session_for(ctx) as conn:
            await store.block_todo(conn, ctx, todo_id=t.id, reason="   ")


async def test_reopen_todo_clears_completed_and_blocked(app_engine):
    """reopen_todo → open, clearing both completed_at and blocked_reason."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Done then reopened")
    async with session_for(ctx) as conn:
        await store.complete_todo(conn, ctx, todo_id=t.id)

    async with session_for(ctx) as conn:
        reopened, previous = await store.reopen_todo(conn, ctx, todo_id=t.id)
    assert reopened.status == "open"
    assert reopened.completed_at is None  # cleared
    assert reopened.blocked_reason is None
    assert previous == "done"


async def test_cancel_todo_sets_cancelled_and_completed_at(app_engine):
    """cancel_todo → cancelled and stamps completed_at; the audit row carries no PII detail
    (a cancellation takes no reason)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Won't do")

    async with session_for(ctx) as conn:
        cancelled, previous = await store.cancel_todo(conn, ctx, todo_id=t.id)
    assert cancelled.status == "cancelled"
    assert cancelled.completed_at is not None
    assert previous == "open"

    # The cancel audit row carries no free-text/PII detail (it is None).
    async with session_for(ctx) as conn:
        rows = (
            await conn.execute(
                select(todo_activity.c.detail).where(
                    (todo_activity.c.todo_id == t.id) & (todo_activity.c.action == "cancel")
                )
            )
        ).all()
    assert rows  # a cancel audit row exists
    assert rows[-1][0] is None


async def test_transition_unknown_todo_raises_not_found(app_engine):
    """Every transition verb on an id not in scope raises TodoNotFoundError (404)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    for verb in (store.start_todo, store.complete_todo, store.reopen_todo, store.cancel_todo):
        with pytest.raises(TodoNotFoundError):
            async with session_for(ctx) as conn:
                await verb(conn, ctx, todo_id="td_missing")
    with pytest.raises(TodoNotFoundError):
        async with session_for(ctx) as conn:
            await store.block_todo(conn, ctx, todo_id="td_missing", reason="r")


# --------------------------------------------------------------------------- #
# 6. reorder_todos: sets positions; validates lane membership                  #
# --------------------------------------------------------------------------- #


async def test_reorder_todos_sets_positions(app_engine):
    """reorder_todos sets each to-do's sort_order to its index in the supplied list."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    ids = []
    async with session_for(ctx) as conn:
        for i in range(3):
            t, _ = await store.create_todo(conn, ctx, title=f"T{i}")
            ids.append(t.id)
    board_id = await _board_id_of(ctx, ids[0])

    # Reverse the order.
    reversed_ids = list(reversed(ids))
    async with session_for(ctx) as conn:
        count = await store.reorder_todos(
            conn, ctx, board_id=board_id, status="open", todo_ids=reversed_ids
        )
    assert count == 3

    # Positions now follow the new list order.
    async with session_for(ctx) as conn:
        rows = {
            r[0]: r[1] for r in (await conn.execute(select(todo.c.id, todo.c.sort_order))).all()
        }
    assert [rows[i] for i in reversed_ids] == [0, 1, 2]


async def test_reorder_todos_rejects_foreign_lane_member(app_engine):
    """An id that is not on the board/status lane raises TodoNotFoundError — a
    reorder can't move a to-do it shouldn't (no silent cross-lane move)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t_todo, _ = await store.create_todo(conn, ctx, title="In open")
        t_doing, _ = await store.create_todo(conn, ctx, title="Will be doing")
    board_id = await _board_id_of(ctx, t_todo.id)
    async with session_for(ctx) as conn:
        await store.start_todo(conn, ctx, todo_id=t_doing.id)  # now in_progress, not open

    with pytest.raises(TodoNotFoundError):
        async with session_for(ctx) as conn:
            await store.reorder_todos(
                conn,
                ctx,
                board_id=board_id,
                status="open",
                todo_ids=[t_todo.id, t_doing.id],  # t_doing is not in the open lane
            )


async def test_reorder_todos_unknown_board_raises_not_found(app_engine):
    """reorder_todos against a board not in scope raises BoardNotFoundError (404)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(BoardNotFoundError):
        async with session_for(ctx) as conn:
            await store.reorder_todos(conn, ctx, board_id="bd_nope", status="open", todo_ids=[])


async def test_reorder_todos_rejects_duplicate_ids(app_engine):
    """A duplicate id in todo_ids is rejected (ambiguous position + count)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Dup")
    board_id = await _board_id_of(ctx, t.id)
    with pytest.raises(ValueError, match="duplicate"):
        async with session_for(ctx) as conn:
            await store.reorder_todos(
                conn, ctx, board_id=board_id, status="open", todo_ids=[t.id, t.id]
            )


# --------------------------------------------------------------------------- #
# 7. archive_todo: sets archived_at; row still present                         #
# --------------------------------------------------------------------------- #


async def test_archive_todo_sets_archived_at_without_deleting(app_engine):
    """archive_todo stamps archived_at and leaves status unchanged; the row is still
    present (soft-hide, never a delete)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Archive me")
    async with session_for(ctx) as conn:
        await store.start_todo(conn, ctx, todo_id=t.id)

    async with session_for(ctx) as conn:
        archived = await store.archive_todo(conn, ctx, todo_id=t.id)
    assert archived.archived_at is not None
    assert archived.status == "in_progress"  # unchanged

    # The row remains in the table.
    assert await _count(ctx, todo, todo.c.id == t.id) == 1


# --------------------------------------------------------------------------- #
# 8. Boards: create / update / list                                            #
# --------------------------------------------------------------------------- #


async def test_create_board_defaults_columns(app_engine):
    """create_board mints a board with the four canonical lanes when columns are omitted."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        b = await store.create_board(conn, ctx, name="Sprint board")
    assert b.id.startswith("bd_")
    assert b.columns is not None
    assert [c["status"] for c in b.columns] == ["open", "in_progress", "blocked", "done"]


async def test_the_first_board_made_is_the_default(app_engine):
    """In a workspace with no default board, create_board's board becomes it; later boards
    do not, and a to-do that names no board lands on the first one."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        first = await store.create_board(conn, ctx, name="Launch")
        second = await store.create_board(conn, ctx, name="Hiring")
        landed, _ = await store.create_todo(conn, ctx, title="no board named")
    assert (first.is_default, second.is_default) == (True, False)
    assert landed.board_id == first.id
    assert await _count(ctx, board) == 2
    assert await _count(ctx, board, board.c.is_default.is_(True)) == 1


async def test_a_board_made_after_the_default_board_is_not_the_default(app_engine):
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        made, _ = await store.create_todo(conn, ctx, title="makes the default board")
        later = await store.create_board(conn, ctx, name="Later")
    assert later.is_default is False
    async with session_for(ctx) as conn:
        again, _ = await store.create_todo(conn, ctx, title="still the default")
    assert again.board_id == made.board_id


async def _unmark_defaults(ctx) -> None:
    """Leave the workspace with boards but no default, as boards made before the first board
    became the default were."""
    async with session_for(ctx) as conn:
        await conn.execute(board.update().values(is_default=False))


async def test_an_only_board_without_a_default_is_the_one_a_write_uses_and_marks(app_engine):
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        only = await store.create_board(conn, ctx, name="Launch")
    await _unmark_defaults(ctx)
    async with session_for(ctx) as conn:
        landed, _ = await store.create_todo(conn, ctx, title="no board named")
    assert landed.board_id == only.id
    assert await _count(ctx, board) == 1
    # Marked, so it stays the default once a second board exists.
    assert await _count(ctx, board, board.c.is_default.is_(True)) == 1
    async with session_for(ctx) as conn:
        await store.create_board(conn, ctx, name="Second")
        again, _ = await store.create_todo(conn, ctx, title="still there")
    assert again.board_id == only.id


async def test_a_second_board_marks_an_unmarked_only_board_rather_than_itself(app_engine):
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        only = await store.create_board(conn, ctx, name="Launch")
    await _unmark_defaults(ctx)
    async with session_for(ctx) as conn:
        second = await store.create_board(conn, ctx, name="Second")
        landed, _ = await store.create_todo(conn, ctx, title="no board named")
    assert second.is_default is False
    assert landed.board_id == only.id


async def test_several_boards_and_no_default_make_the_default_board(app_engine):
    """With no default and several boards, none is the one meant: a to-do that names no board
    gets the default board, as on first use."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.create_board(conn, ctx, name="Launch")
        await store.create_board(conn, ctx, name="Hiring")
    await _unmark_defaults(ctx)
    async with session_for(ctx) as conn:
        landed, _ = await store.create_todo(conn, ctx, title="no board named")
    assert landed.key == "TODO-1"
    assert await _count(ctx, board) == 3


async def test_create_board_accepts_custom_columns(app_engine):
    """create_board stores caller-supplied columns verbatim."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    custom = [
        {"key": "backlog", "label": "Backlog", "status": "open"},
        {"key": "active", "label": "Active", "status": "in_progress"},
    ]
    async with session_for(ctx) as conn:
        b = await store.create_board(conn, ctx, name="Custom", columns=custom)
    assert b.columns == custom


async def test_update_board_is_partial_and_404(app_engine):
    """update_board applies only provided fields; bumps updated_at; 404 if absent."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        b = await store.create_board(conn, ctx, name="Before", description="d")
    before_updated_at = b.updated_at

    async with session_for(ctx) as conn:
        updated = await store.update_board(conn, ctx, board_id=b.id, name="After")
    assert updated.name == "After"  # provided → updated
    assert updated.description == "d"  # omitted → unchanged
    assert updated.updated_at >= before_updated_at

    with pytest.raises(BoardNotFoundError):
        async with session_for(ctx) as conn:
            await store.update_board(conn, ctx, board_id="bd_nope", name="x")


async def test_list_boards_returns_all_in_scope(app_engine):
    """list_boards returns every board in scope (the auto default + created ones),
    newest first."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    # Auto-create the default board via a to-do.
    async with session_for(ctx) as conn:
        await store.create_todo(conn, ctx, title="seed")
    async with session_for(ctx) as conn:
        await store.create_board(conn, ctx, name="Second")
        await store.create_board(conn, ctx, name="Third")

    async with session_for(ctx) as conn:
        boards = await store.list_boards(conn, ctx)
    names = [b.name for b in boards]
    assert set(names) == {"To-dos", "Second", "Third"}
    # Newest first: the two created after the default appear before it.
    assert names[-1] == "To-dos"


# --------------------------------------------------------------------------- #
# 9. Audit: mutations write a todo_activity row                                #
# --------------------------------------------------------------------------- #


async def test_mutations_write_audit_rows(app_engine):
    """A create + a transition each append an outcome="ok" todo_activity row."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Audited")
        await store.complete_todo(conn, ctx, todo_id=t.id)

    async with session_for(ctx) as conn:
        actions = [
            r[0]
            for r in (
                await conn.execute(
                    select(todo_activity.c.action)
                    .where(todo_activity.c.outcome == "ok")
                    .order_by(todo_activity.c.at)
                )
            ).all()
        ]
    assert "create" in actions
    assert "complete" in actions


async def _board_id_of(ctx, todo_id: str) -> str:
    """Helper: read a to-do's board_id directly (test-only convenience)."""
    async with session_for(ctx) as conn:
        row = (await conn.execute(select(todo.c.board_id).where(todo.c.id == todo_id))).first()
    assert row is not None
    return row[0]


# --------------------------------------------------------------------------- #
# 10. list_todos: filters, include_archived, keyset pagination round-trip       #
# --------------------------------------------------------------------------- #


async def test_list_todos_returns_newest_first(app_engine):
    """list_todos orders newest-first (created_at DESC, id DESC) with no filters."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    ids = []
    for i in range(3):
        async with session_for(ctx) as conn:
            t, _ = await store.create_todo(conn, ctx, title=f"T{i}")
        ids.append(t.id)

    async with session_for(ctx) as conn:
        page, cursor = await store.list_todos(conn, ctx)
    # Newest (last created) first.
    assert [t.id for t in page] == list(reversed(ids))
    assert cursor is None  # one short page → no next cursor


async def test_list_todos_filters_by_status_assignee_parent(app_engine):
    """status / assignee / parent_id filters are AND-combined and exact-match."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        goal, _ = await store.create_todo(conn, ctx, title="Goal", assignee="agent")
        child_a, _ = await store.create_todo(conn, ctx, title="Child A", parent_id=goal.id)
        child_b, _ = await store.create_todo(conn, ctx, title="Child B", parent_id=goal.id)
        other, _ = await store.create_todo(conn, ctx, title="Unrelated", assignee="human")
    # Move one child into in_progress.
    async with session_for(ctx) as conn:
        await store.start_todo(conn, ctx, todo_id=child_a.id)

    # parent_id filter → only the two children.
    async with session_for(ctx) as conn:
        children, _ = await store.list_todos(conn, ctx, parent_id=goal.id)
    assert {t.id for t in children} == {child_a.id, child_b.id}

    # status filter → only the in_progress child.
    async with session_for(ctx) as conn:
        in_progress, _ = await store.list_todos(conn, ctx, status="in_progress")
    assert [t.id for t in in_progress] == [child_a.id]

    # assignee filter.
    async with session_for(ctx) as conn:
        by_agent, _ = await store.list_todos(conn, ctx, assignee="agent")
    assert [t.id for t in by_agent] == [goal.id]
    async with session_for(ctx) as conn:
        by_human, _ = await store.list_todos(conn, ctx, assignee="human")
    assert [t.id for t in by_human] == [other.id]


async def test_list_todos_filters_by_label_via_containment(app_engine):
    """label filter matches to-dos whose labels JSONB array CONTAINS the value."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        bug, _ = await store.create_todo(conn, ctx, title="Bug", labels=["bug", "urgent"])
        feat, _ = await store.create_todo(conn, ctx, title="Feature", labels=["feature"])
        await store.create_todo(conn, ctx, title="No labels")

    async with session_for(ctx) as conn:
        tagged, _ = await store.list_todos(conn, ctx, label="bug")
    assert [t.id for t in tagged] == [bug.id]

    # A label only one to-do carries.
    async with session_for(ctx) as conn:
        feats, _ = await store.list_todos(conn, ctx, label="feature")
    assert [t.id for t in feats] == [feat.id]

    # A label nobody has → empty.
    async with session_for(ctx) as conn:
        none, _ = await store.list_todos(conn, ctx, label="missing")
    assert none == []


async def test_list_todos_filters_by_board(app_engine):
    """board_id filter scopes the listing to one board."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        # The to-do first, so it makes the default board and "Other" is a second board.
        on_default, _ = await store.create_todo(conn, ctx, title="Default board todo")
        other_board = await store.create_board(conn, ctx, name="Other")
        on_other, _ = await store.create_todo(
            conn, ctx, title="Other board todo", board_id=other_board.id
        )

    async with session_for(ctx) as conn:
        only_other, _ = await store.list_todos(conn, ctx, board_id=other_board.id)
    assert [t.id for t in only_other] == [on_other.id]
    assert on_default.id not in {t.id for t in only_other}


async def test_list_todos_hides_archived_by_default(app_engine):
    """include_archived=False (default) hides archived to-dos; True surfaces them."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        live, _ = await store.create_todo(conn, ctx, title="Live")
        gone, _ = await store.create_todo(conn, ctx, title="Gone")
    async with session_for(ctx) as conn:
        await store.archive_todo(conn, ctx, todo_id=gone.id)

    async with session_for(ctx) as conn:
        default_view, _ = await store.list_todos(conn, ctx)
    assert [t.id for t in default_view] == [live.id]  # archived dropped out

    async with session_for(ctx) as conn:
        with_archived, _ = await store.list_todos(conn, ctx, include_archived=True)
    assert {t.id for t in with_archived} == {live.id, gone.id}


async def test_list_todos_cursor_walk_meets_every_todo_once(app_engine):
    """A cursor walk yields every to-do once while the board changes between pages.

    The to-dos are created in ONE transaction, so they share ``created_at`` and only the id
    tiebreak orders them. After the first page a new to-do arrives (newer, so it sorts ahead
    of the walk) and two are archived: one already read, one not yet reached.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        created_ids = [
            (await store.create_todo(conn, ctx, title=f"Todo {i}"))[0].id for i in range(7)
        ]
    read_already, not_reached = max(created_ids), min(created_ids)

    async def between(page_number: int) -> None:
        if page_number != 1:
            return
        async with session_for(ctx) as conn:
            await store.create_todo(conn, ctx, title="Late")
            await conn.execute(
                todo.update()
                .where(todo.c.id.in_([read_already, not_reached]))
                .values(archived_at=func.now())
            )

    async def fetch(cursor: str | None) -> tuple[list[Any], str | None]:
        async with session_for(ctx) as conn:
            return await store.list_todos(conn, ctx, limit=3, cursor=cursor)

    expected = set(created_ids) - {not_reached}
    walked = await walk(fetch, ident=lambda t: t.id, expected=expected, between_pages=between)
    assert [t.id for t in walked] == sorted(expected, reverse=True)  # newest first, id tiebreak


async def test_list_todos_clamps_limit(app_engine):
    """limit is clamped to _MAX_LIMIT (a huge limit never over-fetches)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.create_todo(conn, ctx, title="Only one")
    async with session_for(ctx) as conn:
        page, cursor = await store.list_todos(conn, ctx, limit=10_000)
    assert len(page) == 1
    assert cursor is None


async def test_list_todos_invalid_cursor_raises(app_engine):
    """A malformed cursor raises (→ invalid_request at the tool layer), never mis-pages."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.create_todo(conn, ctx, title="x")
    for cursor in ("!!!not-a-valid-cursor!!!", "W10"):
        with pytest.raises(InvalidCursor):
            async with session_for(ctx) as conn:
                await store.list_todos(conn, ctx, cursor=cursor)


# --------------------------------------------------------------------------- #
# 11. get_todo: composite (steps + counts + recent_activity); 404            #
# --------------------------------------------------------------------------- #


async def test_get_todo_composite(app_engine):
    """get_todo returns the to-do, its direct steps (ordered), counts, and recent
    activity (reverse-chron)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        goal, _ = await store.create_todo(conn, ctx, title="Goal")
        s1, _ = await store.create_todo(conn, ctx, title="Step 1", parent_id=goal.id)
        s2, _ = await store.create_todo(conn, ctx, title="Step 2", parent_id=goal.id)
    # Complete one step so steps_done counts it; this also writes activity on s1.
    async with session_for(ctx) as conn:
        await store.complete_todo(conn, ctx, todo_id=s1.id)

    async with session_for(ctx) as conn:
        todo_obj, steps, counts, recent = await store.get_todo(conn, ctx, todo_id=goal.id)

    assert todo_obj.id == goal.id
    assert {t.id for t in steps} == {s1.id, s2.id}
    assert counts == {"steps": 2, "steps_done": 1}

    # The goal's own recent activity carries its create row (most recent first).
    async with session_for(ctx) as conn:
        own, _steps, _counts, own_recent = await store.get_todo(conn, ctx, todo_id=s1.id)
    actions = [a.action for a in own_recent]
    assert actions[0] == "complete"  # reverse-chron: newest first
    assert "create" in actions


async def test_get_todo_unknown_raises_not_found(app_engine):
    """get_todo on an id not in scope raises TodoNotFoundError (404, not 403)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(TodoNotFoundError):
        async with session_for(ctx) as conn:
            await store.get_todo(conn, ctx, todo_id="td_missing")


async def test_get_todo_no_steps(app_engine):
    """A leaf to-do has zero steps and a clean counts dict."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Leaf")
    async with session_for(ctx) as conn:
        _todo, steps, counts, _recent = await store.get_todo(conn, ctx, todo_id=t.id)
    assert steps == []
    assert counts == {"steps": 0, "steps_done": 0}


# --------------------------------------------------------------------------- #
# 12. get_next_todo: priority ordering; skips blocked/done/archived; board filter   #
# --------------------------------------------------------------------------- #


async def test_get_next_todo_picks_highest_priority_actionable(app_engine):
    """get_next_todo returns the highest-priority open todo: urgent before normal."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.create_todo(conn, ctx, title="Low", priority="low")
        await store.create_todo(conn, ctx, title="Normal", priority="normal")
        urgent, _ = await store.create_todo(conn, ctx, title="Urgent", priority="urgent")
        await store.create_todo(conn, ctx, title="High", priority="high")

    async with session_for(ctx) as conn:
        nxt = await store.get_next_todo(conn, ctx)
    assert nxt is not None
    assert nxt.id == urgent.id


async def test_get_next_todo_skips_blocked_done_archived(app_engine):
    """get_next_todo only returns an actionable (open, non-archived) to-do — never a
    blocked / done / cancelled / in_progress / archived one."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        blocked, _ = await store.create_todo(conn, ctx, title="Blocked", priority="urgent")
        done, _ = await store.create_todo(conn, ctx, title="Done", priority="urgent")
        archived, _ = await store.create_todo(conn, ctx, title="Archived", priority="urgent")
        open, _ = await store.create_todo(conn, ctx, title="Actionable", priority="normal")
    async with session_for(ctx) as conn:
        await store.block_todo(conn, ctx, todo_id=blocked.id, reason="waiting")
        await store.complete_todo(conn, ctx, todo_id=done.id)
        await store.archive_todo(conn, ctx, todo_id=archived.id)

    async with session_for(ctx) as conn:
        nxt = await store.get_next_todo(conn, ctx)
    # Even though the urgent ones outrank it, only the plain open is actionable.
    assert nxt is not None
    assert nxt.id == open.id


async def test_get_next_todo_none_when_nothing_actionable(app_engine):
    """With no open to-dos, get_next_todo returns None (not an error)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Only one")
    async with session_for(ctx) as conn:
        await store.start_todo(conn, ctx, todo_id=t.id)  # now in_progress, not open

    async with session_for(ctx) as conn:
        nxt = await store.get_next_todo(conn, ctx)
    assert nxt is None


async def test_get_next_todo_respects_board_filter(app_engine):
    """get_next_todo(board_id=...) only considers that board's actionable to-dos."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        board_a = await store.create_board(conn, ctx, name="A")
        board_b = await store.create_board(conn, ctx, name="B")
        on_a, _ = await store.create_todo(
            conn, ctx, title="On A", board_id=board_a.id, priority="low"
        )
        await store.create_todo(conn, ctx, title="On B", board_id=board_b.id, priority="urgent")

    async with session_for(ctx) as conn:
        nxt = await store.get_next_todo(conn, ctx, board_id=board_a.id)
    assert nxt is not None
    assert nxt.id == on_a.id  # the urgent one on board B is out of scope of the filter


async def test_get_next_todo_priority_tie_breaks_on_sort_order(app_engine):
    """Two same-priority to-dos tie-break on sort_order (lane position) ascending."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        first, _ = await store.create_todo(conn, ctx, title="First", priority="high")
        second, _ = await store.create_todo(conn, ctx, title="Second", priority="high")
    # Both order 0,1 in the open lane; first has the lower sort_order.
    async with session_for(ctx) as conn:
        nxt = await store.get_next_todo(conn, ctx)
    assert nxt is not None
    assert nxt.id == first.id


# --------------------------------------------------------------------------- #
# 13. get_board_summary: lanes, totals, attention; no-default-board 404            #
# --------------------------------------------------------------------------- #


async def test_get_board_summary_lane_counts_and_totals(app_engine):
    """get_board_summary reports per-lane active counts and the active total."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t1, _ = await store.create_todo(conn, ctx, title="Todo 1")
        await store.create_todo(conn, ctx, title="Todo 2")
        doing, _ = await store.create_todo(conn, ctx, title="Doing")
        blocked, _ = await store.create_todo(conn, ctx, title="Blocked")
    async with session_for(ctx) as conn:
        await store.start_todo(conn, ctx, todo_id=doing.id)
        await store.block_todo(conn, ctx, todo_id=blocked.id, reason="stuck")

    async with session_for(ctx) as conn:
        board_obj, lanes, totals, attention = await store.get_board_summary(conn, ctx)

    assert board_obj is not None and board_obj.is_default is True
    lane_counts = {lane["status"]: lane["count"] for lane in lanes}
    assert lane_counts["open"] == 2
    assert lane_counts["in_progress"] == 1
    assert lane_counts["blocked"] == 1
    assert lane_counts["done"] == 0
    assert totals["active"] == 4
    assert totals["blocked"] == 1
    assert attention["blocked"] == 1


async def test_get_board_summary_overdue_and_stalled(app_engine):
    """overdue counts past-due non-terminal to-dos; stalled counts old in_progress."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    past = datetime(2020, 1, 1, tzinfo=UTC)
    async with session_for(ctx) as conn:
        overdue_todo, _ = await store.create_todo(conn, ctx, title="Overdue", due_at=past)
        stalled_todo, _ = await store.create_todo(conn, ctx, title="Stalled")
    async with session_for(ctx) as conn:
        await store.start_todo(conn, ctx, todo_id=stalled_todo.id)
    # Force the stalled to-do's updated_at well into the past (older than the threshold).
    async with session_for(ctx) as conn:
        await conn.execute(
            todo.update()
            .where(todo.c.id == stalled_todo.id)
            .values(updated_at=datetime(2020, 1, 1, tzinfo=UTC))
        )

    async with session_for(ctx) as conn:
        _board, _lanes, totals, attention = await store.get_board_summary(conn, ctx)
    assert totals["overdue"] == 1
    assert attention["overdue"] == 1
    assert totals["stalled"] == 1
    assert attention["stalled"] == 1
    # A completed overdue to-do is NOT counted as overdue (terminal status).
    async with session_for(ctx) as conn:
        await store.complete_todo(conn, ctx, todo_id=overdue_todo.id)
    async with session_for(ctx) as conn:
        _b, _l, totals2, _a = await store.get_board_summary(conn, ctx)
    assert totals2["overdue"] == 0


async def test_get_board_summary_archived_excluded_from_counts(app_engine):
    """Archived to-dos drop out of the active lane counts and totals."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.create_todo(conn, ctx, title="Live")
        gone, _ = await store.create_todo(conn, ctx, title="Gone")
    async with session_for(ctx) as conn:
        await store.archive_todo(conn, ctx, todo_id=gone.id)

    async with session_for(ctx) as conn:
        _board, lanes, totals, _attention = await store.get_board_summary(conn, ctx)
    todo_count = next(lane["count"] for lane in lanes if lane["status"] == "open")
    assert todo_count == 1
    assert totals["active"] == 1


async def test_get_board_summary_active_excludes_terminal(app_engine):
    """totals.active counts only non-terminal work — done/cancelled don't inflate it,
    though they still show in their own lane."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.create_todo(conn, ctx, title="Open 1")
        await store.create_todo(conn, ctx, title="Open 2")
        finished, _ = await store.create_todo(conn, ctx, title="Finished")
    async with session_for(ctx) as conn:
        await store.complete_todo(conn, ctx, todo_id=finished.id)

    async with session_for(ctx) as conn:
        _board, lanes, totals, _attention = await store.get_board_summary(conn, ctx)
    assert totals["active"] == 2  # the two open to-dos, not the done to-do
    done_count = next(lane["count"] for lane in lanes if lane["status"] == "done")
    assert done_count == 1  # the done to-do is still counted in its lane


async def test_get_board_summary_surfaces_wip(app_engine):
    """A column with a wip_limit gets a wip_ok flag (advisory breach signal)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    columns = [
        {"key": "open", "label": "Open", "status": "open", "wip_limit": 1},
        {"key": "doing", "label": "Doing", "status": "in_progress"},
    ]
    async with session_for(ctx) as conn:
        b = await store.create_board(conn, ctx, name="WIP", columns=columns)
        await store.create_todo(conn, ctx, title="One", board_id=b.id)
        await store.create_todo(conn, ctx, title="Two", board_id=b.id)

    async with session_for(ctx) as conn:
        _board, lanes, _totals, _attention = await store.get_board_summary(conn, ctx, board_id=b.id)
    todo_lane = next(lane for lane in lanes if lane["status"] == "open")
    assert todo_lane["wip_limit"] == 1
    assert todo_lane["count"] == 2
    assert todo_lane["wip_ok"] is False  # 2 > 1, an advisory breach


async def test_get_board_summary_explicit_board_id(app_engine):
    """get_board_summary(board_id=...) summarizes that board; 404 on unknown."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        b = await store.create_board(conn, ctx, name="Explicit")
        await store.create_todo(conn, ctx, title="On it", board_id=b.id)

    async with session_for(ctx) as conn:
        board_obj, _lanes, totals, _attention = await store.get_board_summary(
            conn, ctx, board_id=b.id
        )
    assert board_obj is not None and board_obj.id == b.id
    assert totals["active"] == 1

    with pytest.raises(BoardNotFoundError):
        async with session_for(ctx) as conn:
            await store.get_board_summary(conn, ctx, board_id="bd_nope")


async def test_get_board_summary_of_an_empty_workspace_is_empty_and_creates_nothing(app_engine):
    """With no board_id and no board at all, get_board_summary returns no board, no lanes and
    zero counts, and does NOT auto-create a board (a read is side-effect free)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    assert await _count(ctx, board) == 0

    async with session_for(ctx) as conn:
        board_obj, lanes, totals, attention = await store.get_board_summary(conn, ctx)
    assert (board_obj, lanes) == (None, [])
    assert totals == {"active": 0, "overdue": 0, "stalled": 0, "blocked": 0}
    assert attention == {"overdue": 0, "stalled": 0, "blocked": 0}
    assert await _count(ctx, board) == 0


async def test_get_board_summary_reads_an_unmarked_only_board_without_marking_it(app_engine):
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        only = await store.create_board(conn, ctx, name="Launch")
    await _unmark_defaults(ctx)
    async with session_for(ctx) as conn:
        board_obj, *_ = await store.get_board_summary(conn, ctx)
    assert board_obj is not None and board_obj.id == only.id
    assert await _count(ctx, board, board.c.is_default.is_(True)) == 0


async def test_get_board_summary_with_several_boards_and_no_default_is_not_found(app_engine):
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.create_board(conn, ctx, name="Launch")
        await store.create_board(conn, ctx, name="Hiring")
    await _unmark_defaults(ctx)
    with pytest.raises(BoardNotFoundError):
        async with session_for(ctx) as conn:
            await store.get_board_summary(conn, ctx)
    assert await _count(ctx, board) == 2


async def test_has_todos_counts_finished_and_archived_to_dos(app_engine):
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        assert await store.has_todos(conn, ctx) is False
        made, _ = await store.create_todo(conn, ctx, title="done", status="done")
        await store.archive_todo(conn, ctx, todo_id=made.id)
        assert await store.has_todos(conn, ctx) is True


# --------------------------------------------------------------------------- #
# 14. list_todos(query=...): all-tokens substring; tiers; filters; paging      #
# --------------------------------------------------------------------------- #


async def _search(conn, ctx, query: str, **kwargs: Any) -> list[Any]:
    """Every to-do a query lists, its first page."""
    page, _ = await store.list_todos(conn, ctx, query=query, **kwargs)
    return page


async def test_query_all_tokens_must_match(app_engine):
    """A to-do matches only when EVERY token is a substring of title OR details."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        hit, _ = await store.create_todo(
            conn, ctx, title="Deploy the staging cluster", details="kubernetes rollout"
        )
        await store.create_todo(conn, ctx, title="Deploy docs", details="astro build")

    # Both tokens present (one in title, one in details) → matches the first only.
    async with session_for(ctx) as conn:
        results = await _search(conn, ctx, "deploy kubernetes")
    assert [t.id for t in results] == [hit.id]


async def test_query_case_insensitive_substring(app_engine):
    """A query is case-insensitive and substring (not whole-word)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Refactoring the parser")
    async with session_for(ctx) as conn:
        results = await _search(conn, ctx, "REFACTOR")
    assert [r.id for r in results] == [t.id]


async def test_query_ranks_exact_title_first(app_engine):
    """Ranking puts an exact title match ahead of an incidental substring hit."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        incidental, _ = await store.create_todo(
            conn, ctx, title="Something about deploy steps", details=""
        )
        exact, _ = await store.create_todo(conn, ctx, title="deploy")

    async with session_for(ctx) as conn:
        results = await _search(conn, ctx, "deploy")
    assert results[0].id == exact.id  # exact title outranks the incidental substring
    assert {r.id for r in results} == {exact.id, incidental.id}


async def test_query_orders_by_tier_then_newest(app_engine):
    """Exact title > title prefix > every token starts a title word > any other match,
    newest first within a tier, whatever order the to-dos were written in."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        details_only, _ = await store.create_todo(
            conn, ctx, title="Unrelated", details="ship the release"
        )
        mid_word, _ = await store.create_todo(conn, ctx, title="Reshipping release notes")
        word_old, _ = await store.create_todo(conn, ctx, title="The release we ship")
        prefix, _ = await store.create_todo(conn, ctx, title="Ship release now")
        word_new, _ = await store.create_todo(conn, ctx, title="Next\trelease to ship")
        exact, _ = await store.create_todo(conn, ctx, title="SHIP RELEASE")

    async with session_for(ctx) as conn:
        results = await _search(conn, ctx, "ship release")
    assert [r.id for r in results] == [
        exact.id,
        prefix.id,
        word_new.id,
        word_old.id,
        mid_word.id,
        details_only.id,
    ]


async def test_query_pages_by_cursor_in_tier_order(app_engine):
    """Paging a query by its cursor visits every match once, in the one-page order."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        for i in range(4):
            await store.create_todo(conn, ctx, title=f"note {i}", details="alpha")
            await store.create_todo(conn, ctx, title=f"alpha {i}")
        await store.create_todo(conn, ctx, title="alpha")
        await store.create_todo(conn, ctx, title="beta")

    async with session_for(ctx) as conn:
        whole = await _search(conn, ctx, "alpha")
    assert len(whole) == 9

    paged: list[str] = []
    cursor: str | None = None
    while True:
        async with session_for(ctx) as conn:
            page, cursor = await store.list_todos(conn, ctx, query="alpha", limit=2, cursor=cursor)
        paged += [t.id for t in page]
        if cursor is None:
            break
    assert paged == [t.id for t in whole]


async def test_query_and_plain_cursors_are_not_interchangeable(app_engine):
    """A plain listing's cursor is refused with a query, and a query's without one."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        for i in range(3):
            await store.create_todo(conn, ctx, title=f"alpha {i}")
    async with session_for(ctx) as conn:
        _, plain = await store.list_todos(conn, ctx, limit=1)
        _, searched = await store.list_todos(conn, ctx, query="alpha", limit=1)
    assert plain is not None and searched is not None
    async with session_for(ctx) as conn:
        with pytest.raises(InvalidCursor):
            await store.list_todos(conn, ctx, query="alpha", cursor=plain)
    async with session_for(ctx) as conn:
        with pytest.raises(InvalidCursor):
            await store.list_todos(conn, ctx, cursor=searched)


async def test_query_combines_with_filters(app_engine):
    """A query narrows the other filters: status, board, archived."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        on_default, _ = await store.create_todo(conn, ctx, title="Shared keyword default")
        other = await store.create_board(conn, ctx, name="Other")
        on_other, _ = await store.create_todo(
            conn, ctx, title="Shared keyword other", board_id=other.id
        )
        started, _ = await store.create_todo(conn, ctx, title="keyword started")
        await store.start_todo(conn, ctx, todo_id=started.id)

    async with session_for(ctx) as conn:
        by_board = await _search(conn, ctx, "keyword", board_id=other.id)
        by_status = await _search(conn, ctx, "keyword", status="in_progress")
    assert [r.id for r in by_board] == [on_other.id]
    assert on_default.id not in {r.id for r in by_board}
    assert [r.id for r in by_status] == [started.id]


async def test_query_no_match_returns_empty(app_engine):
    """A query that matches nothing returns an empty page (not an error)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.create_todo(conn, ctx, title="Alpha")
    async with session_for(ctx) as conn:
        page, cursor = await store.list_todos(conn, ctx, query="nonexistent-token")
    assert page == [] and cursor is None


async def test_blank_query_filters_nothing(app_engine):
    """An empty/whitespace query has no tokens: the plain listing, newest first."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        first, _ = await store.create_todo(conn, ctx, title="Anything")
        second, _ = await store.create_todo(conn, ctx, title="Something else")
    async with session_for(ctx) as conn:
        results = await _search(conn, ctx, "   ")
    assert [r.id for r in results] == [second.id, first.id]


async def test_query_escapes_like_wildcards(app_engine):
    """A literal % / _ in the query is matched literally, not as a SQL wildcard."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        literal, _ = await store.create_todo(conn, ctx, title="50% done")
        await store.create_todo(conn, ctx, title="plain text")

    async with session_for(ctx) as conn:
        results = await _search(conn, ctx, "50%")
    # Only the to-do literally containing "50%" matches — "%" is not a wildcard.
    assert [r.id for r in results] == [literal.id]


async def test_query_excludes_archived_unless_asked(app_engine):
    """A query hides archived to-dos by default, as the plain listing does."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        live, _ = await store.create_todo(conn, ctx, title="deploy the service")
        gone, _ = await store.create_todo(conn, ctx, title="deploy the old thing")
    async with session_for(ctx) as conn:
        await store.archive_todo(conn, ctx, todo_id=gone.id)

    async with session_for(ctx) as conn:
        hidden = await _search(conn, ctx, "deploy")
        shown = await _search(conn, ctx, "deploy", include_archived=True)
    assert {t.id for t in hidden} == {live.id}
    assert {t.id for t in shown} == {live.id, gone.id}


# --------------------------------------------------------------------------- #
# 15. Reads are side-effect free: no audit row, no board auto-create            #
# --------------------------------------------------------------------------- #


async def test_reads_write_no_audit_rows(app_engine):
    """A read path emits an access log, never a todo_activity row (the write/read
    split.)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        t, _ = await store.create_todo(conn, ctx, title="Seed")
    before = await _count(ctx, todo_activity)

    # Exercise every read.
    async with session_for(ctx) as conn:
        await store.list_todos(conn, ctx)
        await store.get_todo(conn, ctx, todo_id=t.id)
        await store.get_next_todo(conn, ctx)
        await store.get_board_summary(conn, ctx)
        await store.list_todos(conn, ctx, query="seed")

    after = await _count(ctx, todo_activity)
    assert after == before  # no read wrote an audit row
