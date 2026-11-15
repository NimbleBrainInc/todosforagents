"""A runtime's limits: how many boards and open to-dos a tenant may hold.

``Runtime.limits(scope)`` returns :class:`~mcp_todos.runtime.Limits` or ``None``. The store
counts inside the write's own transaction, after the change, and refuses with
:class:`~mcp_todos.db.store.LimitExceededError`, so a refused write keeps nothing. An open
to-do is one that is neither ``done`` nor ``cancelled`` and not archived; any write that makes
one open counts (a create, a plan, and a start, block or reopen of a finished to-do).

At the tool boundary a refusal is the ``invalid_request`` error with a ``limit`` field, and its
``next_step`` says how to free room or where there are no limits. The tool surface is unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import RUNTIME, make_ctx, session_for
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from tool_errors import error_of

from mcp_todos import server
from mcp_todos.db import store, todo_activity
from mcp_todos.db.dialect import serialize_on
from mcp_todos.db.store import LimitExceededError
from mcp_todos.runtime import Limits, Scope, TodoEvent
from mcp_todos_local.runtime import LOCAL_SCOPE

CTX = make_ctx("tenant-a", workspace_id="ws-1")
TODOS_2 = Limits(max_open_todos=2)
BOARDS_1 = Limits(max_boards=1)


async def _create(title: str, limits: Limits | None = TODOS_2, **kw: Any):
    async with session_for(CTX) as conn:
        todo, _ = await store.create_todo(conn, CTX, title=title, limits=limits, **kw)
    return todo


async def _todo_titles() -> list[str]:
    async with session_for(CTX) as conn:
        page, _ = await store.list_todos(conn, CTX, include_archived=True, limit=200)
    return sorted(t.title for t in page)


async def _move(verb: str, todo_id: str, limits: Limits | None = TODOS_2, **kw: Any):
    async with session_for(CTX) as conn:
        fn = getattr(store, f"{verb}_todo")
        if verb in ("start", "block", "reopen"):
            kw["limits"] = limits
        result = await fn(conn, CTX, todo_id=todo_id, **kw)
    return result[0] if isinstance(result, tuple) else result


# --------------------------------------------------------------------------- #
# Open to-dos                                                                  #
# --------------------------------------------------------------------------- #


async def test_a_create_past_the_open_limit_is_refused_and_keeps_nothing(app_engine) -> None:
    await _create("one")
    await _create("two")
    with pytest.raises(LimitExceededError) as refused:
        await _create("three")
    assert refused.value.limit == "max_open_todos"
    assert refused.value.maximum == 2
    assert await _todo_titles() == ["one", "two"]
    # The refused create claimed no number: the next to-do on the board is still TODO-3.
    third = await _create("three", limits=None)
    assert third.key == "TODO-3"


@pytest.mark.parametrize("finish", ["complete", "cancel", "archive"])
async def test_finishing_or_archiving_frees_room(app_engine, finish: str) -> None:
    first = await _create("one")
    await _create("two")
    await _move(finish, first.id)
    await _create("three")
    assert await _todo_titles() == ["one", "three", "two"]


@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_a_to_do_created_finished_is_not_counted(app_engine, status: str) -> None:
    await _create("one")
    await _create("two")
    await _create("already finished", status=status)
    assert len(await _todo_titles()) == 3


async def test_every_open_status_counts(app_engine) -> None:
    await _create("waiting", status="blocked", blocked_reason="review")
    await _create("doing", status="in_progress")
    with pytest.raises(LimitExceededError):
        await _create("one more")


async def test_an_idempotent_replay_at_the_limit_is_not_refused(app_engine) -> None:
    first = await _create("one", idempotency_key="k1")
    await _create("two")
    async with session_for(CTX) as conn:
        again, action = await store.create_todo(
            conn, CTX, title="one", idempotency_key="k1", limits=TODOS_2
        )
    assert (again.id, action) == (first.id, "matched")


async def test_a_plan_past_the_open_limit_is_refused_whole(app_engine) -> None:
    await _create("one")
    with pytest.raises(LimitExceededError):
        async with session_for(CTX) as conn:
            await store.plan_todos(
                conn, CTX, todos=[{"title": "a"}, {"title": "b"}], limits=TODOS_2
            )
    assert await _todo_titles() == ["one"]
    async with session_for(CTX) as conn:
        _, created = await store.plan_todos(conn, CTX, todos=[{"title": "a"}], limits=TODOS_2)
    assert created == 1


async def test_a_plan_of_finished_steps_is_not_counted(app_engine) -> None:
    await _create("one")
    await _create("two")
    async with session_for(CTX) as conn:
        _, created = await store.plan_todos(
            conn,
            CTX,
            todos=[{"title": "a", "status": "done"}, {"title": "b", "status": "cancelled"}],
            limits=TODOS_2,
        )
    assert created == 2


@pytest.mark.parametrize(
    ("verb", "kw"), [("reopen", {}), ("start", {}), ("block", {"reason": "waiting"})]
)
@pytest.mark.parametrize("finish", ["complete", "cancel"])
async def test_a_move_that_makes_a_finished_to_do_open_counts(
    app_engine, verb: str, kw: dict[str, Any], finish: str
) -> None:
    finished = await _create("finished")
    await _move(finish, finished.id)
    await _create("one")
    await _create("two")
    with pytest.raises(LimitExceededError):
        await _move(verb, finished.id, **kw)
    async with session_for(CTX) as conn:
        kept, *_ = await store.get_todo(conn, CTX, todo_id=finished.id)
    assert kept.status == ("done" if finish == "complete" else "cancelled")


async def test_moves_between_open_statuses_are_not_counted(app_engine) -> None:
    # Over the limit already (it was lowered): moving an open to-do between open statuses
    # adds nothing, so it is never refused.
    first = await _create("one", limits=None)
    await _create("two", limits=None)
    await _create("three", limits=None)
    await _move("start", first.id)
    await _move("block", first.id, reason="waiting")
    await _move("reopen", first.id)


async def test_reopening_an_archived_to_do_is_not_counted(app_engine) -> None:
    hidden = await _create("hidden")
    await _move("complete", hidden.id)
    await _move("archive", hidden.id)
    await _create("one")
    await _create("two")
    reopened = await _move("reopen", hidden.id)
    assert reopened.status == "open" and reopened.archived_at is not None


async def test_no_limits_and_unset_fields_limit_nothing(app_engine) -> None:
    for i in range(3):
        await _create(f"none {i}", limits=None)
    for i in range(3):
        await _create(f"unset {i}", limits=Limits(max_boards=5))
    assert len(await _todo_titles()) == 6


# --------------------------------------------------------------------------- #
# Boards                                                                       #
# --------------------------------------------------------------------------- #


async def test_a_board_past_the_limit_is_refused(app_engine) -> None:
    async with session_for(CTX) as conn:
        await store.create_board(conn, CTX, name="Launch", limits=BOARDS_1)
    with pytest.raises(LimitExceededError) as refused:
        async with session_for(CTX) as conn:
            await store.create_board(conn, CTX, name="Second", limits=BOARDS_1)
    assert refused.value.limit == "max_boards"
    async with session_for(CTX) as conn:
        assert [b.name for b in await store.list_boards(conn, CTX)] == ["Launch"]


async def test_the_default_board_counts_toward_the_board_limit(app_engine) -> None:
    async with session_for(CTX) as conn:
        launch = await store.create_board(conn, CTX, name="Launch", limits=BOARDS_1)
    # No board named: the default board would be a second board.
    with pytest.raises(LimitExceededError) as refused:
        await _create("one", limits=BOARDS_1)
    assert refused.value.limit == "max_boards"
    async with session_for(CTX) as conn:
        assert [b.name for b in await store.list_boards(conn, CTX)] == ["Launch"]
    # Naming the board that exists works.
    await _create("one", limits=BOARDS_1, board_id=launch.key)


async def test_an_existing_default_board_is_reused_at_the_board_limit(app_engine) -> None:
    await _create("one", limits=BOARDS_1)
    await _create("two", limits=BOARDS_1)
    with pytest.raises(LimitExceededError):
        async with session_for(CTX) as conn:
            await store.create_board(conn, CTX, name="Second", limits=BOARDS_1)
    async with session_for(CTX) as conn:
        assert len(await store.list_boards(conn, CTX)) == 1


# --------------------------------------------------------------------------- #
# Concurrency                                                                  #
# --------------------------------------------------------------------------- #


async def test_concurrent_creates_never_pass_the_open_limit(app_engine) -> None:
    limits = Limits(max_open_todos=5)

    async def attempt(i: int) -> bool:
        try:
            await _create(f"c{i}", limits=limits)
        except LimitExceededError:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt(i) for i in range(12)))
    assert sum(outcomes) == 5
    assert len(await _todo_titles()) == 5


async def test_concurrent_board_creates_never_pass_the_board_limit(app_engine) -> None:
    limits = Limits(max_boards=2)

    async def attempt(i: int) -> bool:
        try:
            async with session_for(CTX) as conn:
                await store.create_board(conn, CTX, name=f"Board {i}", limits=limits)
        except LimitExceededError:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt(i) for i in range(8)))
    assert sum(outcomes) == 2
    async with session_for(CTX) as conn:
        assert len(await store.list_boards(conn, CTX)) == 2


async def test_postgres_serializes_limit_checks_on_an_advisory_lock() -> None:
    # SQLite's BEGIN IMMEDIATE already serializes every writer; on Postgres the count runs
    # after a transaction-scoped advisory lock on the tenant.
    executed: list[Any] = []

    async def execute(stmt: Any) -> None:
        executed.append(stmt)

    conn = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), execute=execute)
    await serialize_on(conn, "todos:limits:tenant-a")  # ty: ignore[invalid-argument-type]
    [stmt] = executed
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "pg_advisory_xact_lock(hashtextextended('todos:limits:tenant-a', 0))" in sql

    sqlite_conn = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"), execute=execute)
    await serialize_on(sqlite_conn, "todos:limits:tenant-a")  # ty: ignore[invalid-argument-type]
    assert len(executed) == 1


# --------------------------------------------------------------------------- #
# The tool boundary                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def limited(monkeypatch: pytest.MonkeyPatch) -> list[TodoEvent]:
    """The installed runtime limited to one board and two open to-dos; returns its events."""
    events: list[TodoEvent] = []
    monkeypatch.setattr(RUNTIME, "limits", lambda scope: Limits(max_boards=1, max_open_todos=2))
    monkeypatch.setattr(RUNTIME, "record", lambda event, count=1: events.append(event))
    return events


def test_the_local_runtime_has_no_limits() -> None:
    assert RUNTIME.limits(LOCAL_SCOPE) is None
    assert RUNTIME.limits(Scope("tenant-a", "ws-1")) is None


async def _denials() -> list[str]:
    async with session_for(LOCAL_SCOPE) as conn:
        rows = await conn.execute(
            select(todo_activity.c.action).where(todo_activity.c.outcome == "denied")
        )
        return [r[0] for r in rows]


async def test_a_refused_create_todo_is_invalid_request_with_the_limit(
    app_engine, limited: list[TodoEvent]
) -> None:
    await server.create_todo(title="one")
    await server.create_todo(title="two")
    err = error_of(await server.create_todo(title="three"))
    assert err["code"] == "invalid_request"
    assert err["limit"] == "max_open_todos"
    assert err["retryable"] is False
    assert "2 open to-dos" in err["message"]
    assert "complete, cancel or archive" in err["next_step"].lower()
    assert "locally" in err["next_step"]
    assert TodoEvent.OPEN_TODO_LIMIT_REFUSED in limited
    assert await _denials() == ["create"]


async def test_a_refused_reopen_is_invalid_request_with_the_limit(
    app_engine, limited: list[TodoEvent]
) -> None:
    done = await server.create_todo(title="done", status="done")
    await server.create_todo(title="one")
    await server.create_todo(title="two")
    err = error_of(await server.reopen_todo(todo_id=done["todo"]["id"]))
    assert (err["code"], err["limit"]) == ("invalid_request", "max_open_todos")
    steps: Any = [{"title": "a"}]
    plan = error_of(await server.plan_todos(todos=steps))
    assert plan["limit"] == "max_open_todos"


async def test_a_refused_create_board_is_invalid_request_with_the_limit(
    app_engine, limited: list[TodoEvent]
) -> None:
    await server.create_todo(title="one")  # creates the default board
    err = error_of(await server.create_board(name="Second"))
    assert err["code"] == "invalid_request"
    assert err["limit"] == "max_boards"
    assert "list_boards" in err["next_step"]
    assert "locally" in err["next_step"]
    assert TodoEvent.BOARD_LIMIT_REFUSED in limited
