"""A runtime's limits: how many boards, open to-dos and to-dos in all a tenant may hold, and
how long one to-do's details may be.

``Runtime.limits(scope)`` returns :class:`~mcp_todos.runtime.Limits` or ``None``. The store
counts inside the write's own transaction, after the change, and refuses with
:class:`~mcp_todos.db.store.LimitExceededError`, so a refused write keeps nothing. An open
to-do is one that is neither ``done`` nor ``cancelled`` and not archived; any write that makes
one open counts (a create, a plan, and a start, block or reopen of a finished to-do). The
lifetime total counts every to-do, whatever its status, archived included; every create and
plan counts toward it. The details limit is checked before a create, plan or update writes.

At the tool boundary a refusal is the ``invalid_request`` error with a ``limit`` field, and its
``next_step`` says what to do inside the account: free room, or use the board it has. The tool
surface is unchanged.
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
from mcp_todos.db import board, store, todo_activity
from mcp_todos.db.dialect import serialize_on
from mcp_todos.db.store import LimitExceededError
from mcp_todos.models import PlanItem
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


async def test_a_board_made_first_takes_the_to_dos_that_name_no_board(app_engine) -> None:
    # Board first, then a to-do with no board_id: the board is the default, so no second
    # board is needed and the one-board limit is not reached.
    async with session_for(CTX) as conn:
        launch = await store.create_board(conn, CTX, name="Launch", limits=BOARDS_1)
    made = await _create("one", limits=BOARDS_1)
    assert made.board_id == launch.id
    assert made.key == f"{launch.key}-1"
    async with session_for(CTX) as conn:
        assert [b.name for b in await store.list_boards(conn, CTX)] == ["Launch"]


async def test_the_default_board_counts_toward_the_board_limit(app_engine) -> None:
    # Several boards and none of them the default (made before the first board became it):
    # a to-do that names no board would need a new default board, a third board.
    async with session_for(CTX) as conn:
        await store.create_board(conn, CTX, name="Launch")
        await store.create_board(conn, CTX, name="Hiring")
        await conn.execute(board.update().values(is_default=False))
    with pytest.raises(LimitExceededError) as refused:
        await _create("one", limits=Limits(max_boards=2))
    assert refused.value.limit == "max_boards"
    assert sorted(await _boards()) == ["Hiring", "Launch"]


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


def _record_locks(monkeypatch: pytest.MonkeyPatch, on_default_lock: Any = None) -> list[str]:
    """Record the lock keys the store takes, in order. ``on_default_lock(conn)`` runs once, as
    the first default-board lock is granted, standing in for a write that committed while this
    one waited (on Postgres, READ COMMITTED shows it to every statement after the lock)."""
    taken: list[str] = []
    pending = [on_default_lock] if on_default_lock is not None else []

    async def recording(conn: Any, key: str) -> None:
        taken.append(key)
        if pending and key.startswith("todos:default-board:"):
            await pending.pop()(conn)

    monkeypatch.setattr(store, "serialize_on", recording)
    return taken


DEFAULT_LOCK = f"todos:default-board:{CTX.tenant_id}:{CTX.workspace_id}"
LIMITS_LOCK = f"todos:limits:{CTX.tenant_id}"


@pytest.mark.parametrize("first_write", ["create_board", "create_todo"])
async def test_setting_the_default_board_locks_the_workspace_before_the_limits(
    app_engine, monkeypatch: pytest.MonkeyPatch, first_write: str
) -> None:
    # SQLite serializes every writer anyway, so this pins the order Postgres relies on: the
    # workspace's default-board lock, then the tenant's limits lock, never the reverse.
    taken = _record_locks(monkeypatch)
    async with session_for(CTX) as conn:
        if first_write == "create_board":
            await store.create_board(conn, CTX, name="Launch", limits=BOARDS_1)
        else:
            await store.create_todo(conn, CTX, title="first", limits=BOARDS_1)
    assert taken == [DEFAULT_LOCK, LIMITS_LOCK]


async def test_a_workspace_with_a_default_board_takes_no_default_lock(
    app_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create("makes the default board", limits=None)
    taken = _record_locks(monkeypatch)
    async with session_for(CTX) as conn:
        await store.create_board(conn, CTX, name="Second", limits=Limits(max_boards=2))
    assert taken == [LIMITS_LOCK]


async def test_create_board_decides_the_default_after_the_lock(
    app_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A first board another write made while this one waited for the lock is the default; a
    # decision taken before the lock would make a second default and break uq_board_default.
    async def concurrent_first_use(conn: Any) -> None:
        await store._resolve_default_board(conn, CTX)

    _record_locks(monkeypatch, concurrent_first_use)
    async with session_for(CTX) as conn:
        made = await store.create_board(conn, CTX, name="Launch")
    assert made.is_default is False
    async with session_for(CTX) as conn:
        defaults = (await conn.execute(select(board).where(board.c.is_default.is_(True)))).all()
    assert [row.key for row in defaults] == ["TODO"]


async def test_a_move_reads_the_status_it_counts_on_under_a_row_lock(app_engine) -> None:
    # Under READ COMMITTED, an unlocked read could see a to-do still open after a concurrent
    # complete freed its slot for another create; the move would then skip the count. The read
    # that decides whether to count is SELECT ... FOR UPDATE, taken before the UPDATE.
    finished = await _create("finished")
    await _move("complete", finished.id)
    executed: list[Any] = []
    async with session_for(CTX) as conn:
        real_execute = conn.execute

        async def execute(stmt: Any, *args: Any, **kw: Any) -> Any:
            executed.append(stmt)
            return await real_execute(stmt, *args, **kw)

        proxy = SimpleNamespace(dialect=conn.dialect, execute=execute)
        await store.reopen_todo(proxy, CTX, todo_id=finished.id, limits=TODOS_2)  # ty: ignore[invalid-argument-type]
    sql = [str(stmt.compile(dialect=postgresql.dialect())) for stmt in executed]
    locked = next(i for i, s in enumerate(sql) if s.rstrip().endswith("FOR UPDATE"))
    updated = next(i for i, s in enumerate(sql) if s.startswith("UPDATE todo"))
    assert locked < updated
    assert "todo.status" in sql[locked]


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
    _assert_in_product(err["next_step"])
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


def _assert_in_product(next_step: str) -> None:
    """A refusal's next_step names steps inside this account, never another way to run it."""
    lowered = next_step.lower()
    for elsewhere in ("uvx", "locally", "install", "no limits"):
        assert elsewhere not in lowered, next_step


async def test_a_refused_create_board_is_invalid_request_with_the_limit(
    app_engine, limited: list[TodoEvent]
) -> None:
    # A to-do with no board first, then a board: the implicit default board took the one slot.
    await server.create_todo(title="one")
    err = error_of(await server.create_board(name="Second"))
    assert err["code"] == "invalid_request"
    assert err["limit"] == "max_boards"
    assert err["message"] == "This account is at its limit of 1 board. Nothing was changed."
    # It names the board there is, and how to make it fit: rename it, separate with labels.
    assert '"To-dos" (TODO)' in err["next_step"]
    assert 'board_id="TODO"' in err["next_step"]
    assert "update_board" in err["next_step"]
    assert "labels" in err["next_step"]
    _assert_in_product(err["next_step"])
    assert TodoEvent.BOARD_LIMIT_REFUSED in limited


async def test_a_refused_create_board_names_every_board_there_is(
    app_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(RUNTIME, "limits", lambda scope: Limits(max_boards=2))
    await server.create_board(name="Launch plan", key="LP")
    await server.create_board(name="Hiring", key="HR")
    err = error_of(await server.create_board(name="Third"))
    assert err["limit"] == "max_boards"
    assert '"Launch plan" (LP)' in err["next_step"]
    assert '"Hiring" (HR)' in err["next_step"]
    assert "labels" in err["next_step"]
    _assert_in_product(err["next_step"])


async def test_board_first_then_a_to_do_with_no_board_lands_on_it_at_the_limit(
    app_engine, limited: list[TodoEvent]
) -> None:
    board_made = await server.create_board(name="Launch plan")
    key = board_made["board"]["key"]
    made = await server.create_todo(title="one")
    assert made["todo"]["board_id"] == board_made["board"]["id"]
    assert made["todo"]["key"] == f"{key}-1"
    planned = await server.plan_todos(todos=[PlanItem(title="two")])
    assert planned["todos"][0]["board_id"] == board_made["board"]["id"]
    assert TodoEvent.BOARD_LIMIT_REFUSED not in limited


async def test_list_boards_at_the_board_limit_does_not_suggest_create_board(
    app_engine, limited: list[TodoEvent]
) -> None:
    await server.create_board(name="Launch plan", key="LP")
    listing = await server.list_boards()
    assert "create_board" not in listing["next_step"]
    assert '"Launch plan" (LP)' in listing["next_step"]
    assert "labels" in listing["next_step"]


async def test_list_boards_under_the_board_limit_suggests_create_board(
    app_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(RUNTIME, "limits", lambda scope: Limits(max_boards=2))
    await server.create_board(name="Launch plan", key="LP")
    assert "create_board" in (await server.list_boards())["next_step"]


# --------------------------------------------------------------------------- #
# Lifetime total                                                               #
# --------------------------------------------------------------------------- #

TOTAL_3 = Limits(max_total_todos=3)


async def _boards() -> list[str]:
    async with session_for(CTX) as conn:
        return [b.name for b in await store.list_boards(conn, CTX)]


@pytest.mark.parametrize("status", ["open", "in_progress", "done", "cancelled"])
async def test_a_create_past_the_total_is_refused_whatever_its_status(
    app_engine, status: str
) -> None:
    await _create("one", limits=TOTAL_3)
    await _create("two", limits=TOTAL_3, status="done")
    await _create("three", limits=TOTAL_3, status="cancelled")  # exactly at the total: kept
    with pytest.raises(LimitExceededError) as refused:
        await _create("four", limits=TOTAL_3, status=status)
    assert refused.value.limit == "max_total_todos"
    assert refused.value.maximum == 3
    assert "lifetime allowance of 3 to-dos" in str(refused.value)
    assert await _todo_titles() == ["one", "three", "two"]
    # The refused create claimed no number.
    fourth = await _create("four", limits=None)
    assert fourth.key == "TODO-4"


async def test_finishing_or_archiving_frees_no_room_in_the_total(app_engine) -> None:
    limits = Limits(max_total_todos=2)
    for title in ("one", "two"):
        made = await _create(title, limits=limits)
        await _move("complete", made.id)
    await _move("archive", made.id)
    with pytest.raises(LimitExceededError) as refused:
        await _create("three", limits=limits)
    assert refused.value.limit == "max_total_todos"


async def test_the_total_counts_every_board(app_engine) -> None:
    async with session_for(CTX) as conn:
        launch = await store.create_board(conn, CTX, name="Launch")
    await _create("default board", limits=Limits(max_total_todos=2))
    await _create("launch", limits=Limits(max_total_todos=2), board_id=launch.key)
    with pytest.raises(LimitExceededError):
        await _create("either", limits=Limits(max_total_todos=2), board_id=launch.key)


@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_a_plan_of_finished_steps_counts_toward_the_total(app_engine, status: str) -> None:
    async with session_for(CTX) as conn:
        _, created = await store.plan_todos(
            conn,
            CTX,
            todos=[{"title": t, "status": status} for t in ("a", "b", "c")],
            limits=TOTAL_3,
        )
    assert created == 3  # exactly at the total
    with pytest.raises(LimitExceededError) as refused:
        async with session_for(CTX) as conn:
            await store.plan_todos(
                conn, CTX, todos=[{"title": "d", "status": status}], limits=TOTAL_3
            )
    assert refused.value.limit == "max_total_todos"
    assert await _todo_titles() == ["a", "b", "c"]


async def test_a_plan_past_the_total_is_refused_whole(app_engine) -> None:
    await _create("one", limits=TOTAL_3)
    with pytest.raises(LimitExceededError):
        async with session_for(CTX) as conn:
            await store.plan_todos(
                conn,
                CTX,
                todos=[{"title": "a", "status": "done"}, {"title": "b"}, {"title": "c"}],
                limits=TOTAL_3,
            )
    assert await _todo_titles() == ["one"]
    # Nothing was claimed: the next to-do is still TODO-2.
    second = await _create("two", limits=TOTAL_3)
    assert second.key == "TODO-2"


async def test_an_idempotent_replay_at_the_total_is_not_refused(app_engine) -> None:
    first = await _create("one", limits=TOTAL_3, idempotency_key="k1")
    steps: list[Any] = [{"title": "a", "status": "done"}, {"title": "b", "status": "done"}]
    async with session_for(CTX) as conn:
        await store.plan_todos(conn, CTX, todos=steps, idempotency_key="p", limits=TOTAL_3)
    async with session_for(CTX) as conn:
        again, action = await store.create_todo(
            conn, CTX, title="one", idempotency_key="k1", limits=TOTAL_3
        )
        _, created = await store.plan_todos(
            conn, CTX, todos=steps, idempotency_key="p", limits=TOTAL_3
        )
    assert (again.id, action, created) == (first.id, "matched", 0)


async def test_over_both_counts_the_refusal_names_the_total(app_engine) -> None:
    limits = Limits(max_open_todos=1, max_total_todos=1)
    await _create("one", limits=limits)
    with pytest.raises(LimitExceededError) as refused:
        await _create("two", limits=limits)
    assert refused.value.limit == "max_total_todos"


async def test_the_total_leaves_the_open_count_as_it_was(app_engine) -> None:
    # With room in the total, the open limit still refuses and still frees on completion.
    limits = Limits(max_open_todos=1, max_total_todos=10)
    first = await _create("one", limits=limits)
    with pytest.raises(LimitExceededError) as refused:
        await _create("two", limits=limits)
    assert refused.value.limit == "max_open_todos"
    await _move("complete", first.id)
    await _create("two", limits=limits)
    # A move adds no row, so it never counts toward the total.
    await _move("reopen", first.id, limits=Limits(max_total_todos=2))


async def test_an_unset_total_limits_nothing(app_engine) -> None:
    for i in range(4):
        await _create(f"done {i}", limits=Limits(max_open_todos=1), status="done")
    for i in range(4):
        await _create(f"unset {i}", limits=Limits(), status="cancelled")
    assert len(await _todo_titles()) == 8


async def test_concurrent_creates_never_pass_the_total(app_engine) -> None:
    limits = Limits(max_total_todos=5)

    async def attempt(i: int) -> bool:
        try:
            if i % 3 == 0:
                async with session_for(CTX) as conn:
                    await store.plan_todos(
                        conn, CTX, todos=[{"title": f"p{i}", "status": "done"}], limits=limits
                    )
            else:
                await _create(f"c{i}", limits=limits, status="done" if i % 2 else "open")
        except LimitExceededError:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt(i) for i in range(12)))
    assert sum(outcomes) == 5
    assert len(await _todo_titles()) == 5


# --------------------------------------------------------------------------- #
# Details bytes                                                                #
# --------------------------------------------------------------------------- #

DETAILS_10 = Limits(max_details_bytes=10)


@pytest.mark.parametrize(
    "details",
    ["x" * 10, "é" * 5, "  " + "x" * 10 + "\n"],
    ids=["ascii", "multibyte", "trimmed"],
)
async def test_details_at_the_limit_are_kept(app_engine, details: str) -> None:
    made = await _create("one", limits=DETAILS_10, details=details)
    async with session_for(CTX) as conn:
        await store.update_todo(conn, CTX, todo_id=made.id, details=details, limits=DETAILS_10)
        steps: Any = [{"title": "a", "details": details}]
        await store.plan_todos(conn, CTX, todos=steps, limits=DETAILS_10)


@pytest.mark.parametrize("details", ["x" * 11, "é" * 5 + "x"], ids=["ascii", "multibyte"])
async def test_a_create_with_details_past_the_limit_is_refused(app_engine, details: str) -> None:
    with pytest.raises(LimitExceededError) as refused:
        await _create("one", limits=DETAILS_10, details=details)
    assert (refused.value.limit, refused.value.maximum) == ("max_details_bytes", 10)
    assert str(refused.value).startswith("details exceeds")
    # Refused before anything was written: not even the default board.
    assert await _todo_titles() == []
    assert await _boards() == []


async def test_a_plan_with_one_step_past_the_details_limit_is_refused_whole(app_engine) -> None:
    steps: Any = [{"title": "a", "details": "ok"}, {"title": "b", "details": "x" * 11}]
    with pytest.raises(LimitExceededError) as refused:
        async with session_for(CTX) as conn:
            await store.plan_todos(conn, CTX, todos=steps, limits=DETAILS_10)
    assert refused.value.limit == "max_details_bytes"
    assert str(refused.value).startswith("todos[1].details exceeds")
    assert await _todo_titles() == []


async def test_an_update_with_details_past_the_limit_changes_nothing(app_engine) -> None:
    made = await _create("one", details="short")
    with pytest.raises(LimitExceededError) as refused:
        async with session_for(CTX) as conn:
            await store.update_todo(
                conn, CTX, todo_id=made.id, title="renamed", details="x" * 11, limits=DETAILS_10
            )
    assert refused.value.limit == "max_details_bytes"
    async with session_for(CTX) as conn:
        kept, *_ = await store.get_todo(conn, CTX, todo_id=made.id)
    assert (kept.title, kept.details) == ("one", "short")


async def test_an_update_that_leaves_long_details_alone_is_not_refused(app_engine) -> None:
    # Details stored before the limit was set stay; only a write that sets them is checked.
    made = await _create("one", limits=None, details="x" * 100)
    async with session_for(CTX) as conn:
        updated = await store.update_todo(
            conn, CTX, todo_id=made.id, title="renamed", limits=DETAILS_10
        )
    assert updated.details == "x" * 100


async def test_the_core_details_cap_still_applies_under_a_larger_limit(app_engine) -> None:
    huge = Limits(max_details_bytes=10**7)
    with pytest.raises(ValueError, match="maximum length"):
        await _create("one", limits=huge, details="x" * 65537)
    # And it is the cap, not the limit, that refuses a value past both.
    with pytest.raises(ValueError, match="maximum length"):
        await _create("one", limits=DETAILS_10, details="x" * 65537)
    with pytest.raises(ValueError, match="maximum length"):
        async with session_for(CTX) as conn:
            steps: Any = [{"title": "a", "details": "x" * 65537}]
            await store.plan_todos(conn, CTX, todos=steps, limits=DETAILS_10)


async def test_an_unset_details_limit_limits_nothing(app_engine) -> None:
    await _create("none", limits=None, details="x" * 65536)
    await _create("unset", limits=Limits(max_total_todos=5), details="x" * 65536)
    assert len(await _todo_titles()) == 2


# --------------------------------------------------------------------------- #
# The tool boundary: lifetime total and details                               #
# --------------------------------------------------------------------------- #


@pytest.fixture
def lifetime(monkeypatch: pytest.MonkeyPatch) -> list[TodoEvent]:
    """The installed runtime limited to two to-dos in all and 10 bytes of details."""
    events: list[TodoEvent] = []
    monkeypatch.setattr(
        RUNTIME, "limits", lambda scope: Limits(max_total_todos=2, max_details_bytes=10)
    )
    monkeypatch.setattr(RUNTIME, "record", lambda event, count=1: events.append(event))
    return events


async def test_a_refused_create_past_the_total_is_invalid_request_with_the_limit(
    app_engine, lifetime: list[TodoEvent]
) -> None:
    await server.create_todo(title="one", status="done")
    await server.create_todo(title="two", status="cancelled")
    err = error_of(await server.create_todo(title="three", status="done"))
    assert err == {
        "code": "invalid_request",
        "message": "This account has reached its lifetime allowance of 2 to-dos. "
        "Nothing was changed.",
        "retryable": False,
        "limit": "max_total_todos",
        "next_step": err["next_step"],
    }
    assert "lifetime allowance" in err["next_step"]
    assert "list_todos" in err["next_step"] and "get_todo" in err["next_step"]
    _assert_in_product(err["next_step"])
    assert TodoEvent.TOTAL_TODO_LIMIT_REFUSED in lifetime
    assert TodoEvent.OPEN_TODO_LIMIT_REFUSED not in lifetime
    assert await _denials() == ["create"]
    steps: Any = [{"title": "a", "status": "done"}]
    plan = error_of(await server.plan_todos(todos=steps))
    assert (plan["code"], plan["limit"]) == ("invalid_request", "max_total_todos")


@pytest.mark.parametrize("tool", ["create", "plan", "update"])
async def test_a_refused_details_write_is_invalid_request_with_the_limit(
    app_engine, lifetime: list[TodoEvent], tool: str
) -> None:
    long = "x" * 11
    if tool == "create":
        result = await server.create_todo(title="one", details=long)
        field = "details"
    elif tool == "plan":
        steps: Any = [{"title": "a", "details": long}]
        result = await server.plan_todos(todos=steps)
        field = "todos[0].details"
    else:
        made = await server.create_todo(title="one", details="short")
        result = await server.update_todo(todo_id=made["todo"]["id"], details=long)
        field = "details"
    err = error_of(result)
    assert err == {
        "code": "invalid_request",
        "message": f"{field} exceeds this account's limit of 10 bytes of details per to-do. "
        "Nothing was changed.",
        "retryable": False,
        "limit": "max_details_bytes",
        "next_step": err["next_step"],
    }
    assert "Shorten the details" in err["next_step"]
    _assert_in_product(err["next_step"])
    assert TodoEvent.DETAILS_LIMIT_REFUSED in lifetime
    assert await _denials() == [tool]


@pytest.mark.parametrize(
    ("limit", "maximum", "expected"),
    [
        ("max_boards", 1, "This account is at its limit of 1 board. Nothing was changed."),
        ("max_boards", 3, "This account is at its limit of 3 boards. Nothing was changed."),
        ("max_open_todos", 1, "This account is at its limit of 1 open to-do. Nothing was changed."),
        (
            "max_open_todos",
            25,
            "This account is at its limit of 25 open to-dos. Nothing was changed.",
        ),
        (
            "max_total_todos",
            1,
            "This account has reached its lifetime allowance of 1 to-do. Nothing was changed.",
        ),
        (
            "max_total_todos",
            500,
            "This account has reached its lifetime allowance of 500 to-dos. Nothing was changed.",
        ),
    ],
)
def test_a_limit_message_counts_in_the_right_number(
    limit: str, maximum: int, expected: str
) -> None:
    assert str(LimitExceededError(limit, maximum)) == expected


def test_a_details_limit_of_one_byte_is_singular() -> None:
    assert "limit of 1 byte of details" in str(LimitExceededError("max_details_bytes", 1))
