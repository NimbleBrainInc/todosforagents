"""Board keys, to-do numbers and key resolution in the store."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from conftest import make_ctx, session_for
from sqlalchemy import event

from mcp_todos.db import store
from mcp_todos.db.store import TodoNotFoundError


def _ctx():
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _board(ctx, name: str, key: str | None = None):
    async with session_for(ctx) as conn:
        return await store.create_board(conn, ctx, name=name, key=key)


async def _create(ctx, title: str, **kwargs):
    async with session_for(ctx) as conn:
        todo, _ = await store.create_todo(conn, ctx, title=title, **kwargs)
    return todo


# --------------------------------------------------------------------------- #
# Board keys                                                                   #
# --------------------------------------------------------------------------- #


async def test_a_board_derives_its_key_from_its_name(app_engine) -> None:
    ctx = _ctx()
    assert (await _board(ctx, "Magnificent board")).key == "MAG"
    assert (await _board(ctx, "Launch plan")).key == "LP"
    assert (await _board(ctx, "Magnificent")).key == "MAG2"


async def test_the_default_board_is_todo(app_engine) -> None:
    ctx = _ctx()
    todo = await _create(ctx, "First")
    async with session_for(ctx) as conn:
        [board] = await store.list_boards(conn, ctx)
    assert board.is_default and board.key == "TODO"
    assert todo.key == "TODO-1"


async def test_an_explicit_key_is_kept_uppercased(app_engine) -> None:
    ctx = _ctx()
    board = await _board(ctx, "Anything", key="q4")
    assert board.key == "Q4"
    assert (await _create(ctx, "A", board_id=board.id)).key == "Q4-1"


@pytest.mark.parametrize("key", ["A", "TOOLONG", "1AB", "A-B"])
async def test_an_explicit_key_that_breaks_the_rule_is_refused(app_engine, key: str) -> None:
    ctx = _ctx()
    with pytest.raises(ValueError, match="2-5 characters"):
        await _board(ctx, "Anything", key=key)


async def test_an_explicit_key_already_used_in_the_workspace_is_refused(app_engine) -> None:
    ctx = _ctx()
    await _board(ctx, "Launch")
    with pytest.raises(ValueError, match="already used"):
        await _board(ctx, "Other", key="lau")
    with pytest.raises(ValueError, match="reserved"):
        await _board(ctx, "Other", key="todo")


async def test_a_name_with_every_suffix_taken_asks_for_an_explicit_key(app_engine) -> None:
    ctx = _ctx()
    for _ in range(9):  # LAU, LAU2 ... LAU9
        await _board(ctx, "Launch")
    with pytest.raises(ValueError, match="pass an explicit key"):
        await _board(ctx, "Launch")
    assert (await _board(ctx, "Launch", key="LCH")).key == "LCH"


async def test_update_board_leaves_the_key(app_engine) -> None:
    ctx = _ctx()
    board = await _board(ctx, "Launch")
    async with session_for(ctx) as conn:
        updated = await store.update_board(conn, ctx, board_id=board.id, name="Renamed")
    assert updated.key == "LAU"


# --------------------------------------------------------------------------- #
# Numbers                                                                      #
# --------------------------------------------------------------------------- #


async def test_numbers_are_sequential_per_board(app_engine) -> None:
    ctx = _ctx()
    mag = await _board(ctx, "Magnificent")
    lp = await _board(ctx, "Launch plan")
    keys = [
        (await _create(ctx, "a", board_id=mag.id)).key,
        (await _create(ctx, "b", board_id=lp.id)).key,
        (await _create(ctx, "c", board_id=mag.id)).key,
        (await _create(ctx, "d")).key,
    ]
    assert keys == ["MAG-1", "LP-1", "MAG-2", "TODO-1"]


async def test_archiving_frees_no_number(app_engine) -> None:
    ctx = _ctx()
    first = await _create(ctx, "a")
    async with session_for(ctx) as conn:
        await store.archive_todo(conn, ctx, todo_id=first.id)
    assert (await _create(ctx, "b")).key == "TODO-2"


async def test_a_plan_numbers_its_steps_in_plan_order(app_engine) -> None:
    ctx = _ctx()
    await _create(ctx, "before")
    async with session_for(ctx) as conn:
        planned, _ = await store.plan_todos(
            conn, ctx, todos=[{"title": "one"}, {"title": "two", "status": "done"}, {"title": "3"}]
        )
    assert [t.key for t in planned] == ["TODO-2", "TODO-3", "TODO-4"]
    assert (await _create(ctx, "after")).key == "TODO-5"


async def test_an_idempotent_replay_takes_no_number(app_engine) -> None:
    ctx = _ctx()
    first = await _create(ctx, "once", idempotency_key="k")
    again = await _create(ctx, "once", idempotency_key="k")
    assert again.id == first.id and again.key == "TODO-1"

    async with session_for(ctx) as conn:
        planned, _ = await store.plan_todos(
            conn, ctx, todos=[{"title": "a"}, {"title": "b"}], idempotency_key="p"
        )
    async with session_for(ctx) as conn:
        # The retry adds a third step: only it takes a number.
        replayed, created = await store.plan_todos(
            conn,
            ctx,
            todos=[{"title": "a"}, {"title": "b"}, {"title": "c"}],
            idempotency_key="p",
        )
    assert [t.key for t in planned] == ["TODO-2", "TODO-3"]
    assert created == 1
    assert [t.key for t in replayed] == ["TODO-2", "TODO-3", "TODO-4"]
    assert (await _create(ctx, "next")).key == "TODO-5"


async def test_concurrent_creates_on_one_board_get_distinct_numbers(app_engine) -> None:
    ctx = _ctx()
    board = await _board(ctx, "Race")
    created = await asyncio.gather(*(_create(ctx, f"t{i}", board_id=board.id) for i in range(12)))
    assert sorted(t.key for t in created) == sorted(f"RAC-{i}" for i in range(1, 13))


# --------------------------------------------------------------------------- #
# Resolution: every argument that takes a to-do id takes a key                  #
# --------------------------------------------------------------------------- #


async def test_every_todo_argument_takes_a_key(app_engine) -> None:
    ctx = _ctx()
    board = await _board(ctx, "Magnificent")
    goal = await _create(ctx, "goal", board_id=board.id)  # MAG-1
    step = await _create(ctx, "step", board_id=board.id, parent_id="mag-1")  # MAG-2
    assert step.parent_id == goal.id

    async with session_for(ctx) as conn:
        planned, _ = await store.plan_todos(
            conn, ctx, todos=[{"title": "p"}], board_id=board.id, parent_id="MAG-1"
        )
        assert planned[0].parent_id == goal.id and planned[0].key == "MAG-3"

        todo, steps, counts, _ = await store.get_todo(conn, ctx, todo_id="Mag-1")
        assert todo.id == goal.id and counts["steps"] == 2
        assert {s.key for s in steps} == {"MAG-2", "MAG-3"}

        page, _ = await store.list_todos(conn, ctx, parent_id="MAG-1")
        assert {t.id for t in page} == {step.id, planned[0].id}

    other = await _create(ctx, "other", board_id=board.id)  # MAG-4
    async with session_for(ctx) as conn:
        updated = await store.update_todo(conn, ctx, todo_id="MAG-4", title="x", parent_id="MAG-2")
        assert updated.id == other.id and updated.parent_id == step.id

        for verb, kwargs, status in [
            (store.start_todo, {}, "in_progress"),
            (store.block_todo, {"reason": "r"}, "blocked"),
            (store.reopen_todo, {}, "open"),
            (store.complete_todo, {}, "done"),
            (store.cancel_todo, {}, "cancelled"),
        ]:
            moved, _ = await verb(conn, ctx, todo_id="mag-2", **kwargs)
            assert moved.id == step.id and moved.status == status

        n = await store.reorder_todos(
            conn, ctx, board_id=board.id, status="open", todo_ids=["MAG-4", goal.id]
        )
        assert n == 2
        archived = await store.archive_todo(conn, ctx, todo_id="MAG-4")
        assert archived.id == other.id and archived.archived_at is not None


async def test_a_todo_named_both_ways_in_a_reorder_is_a_duplicate(app_engine) -> None:
    ctx = _ctx()
    todo = await _create(ctx, "a")
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError, match="duplicate"):
            await store.reorder_todos(
                conn, ctx, board_id=todo.board_id, status="open", todo_ids=[todo.id, "TODO-1"]
            )


async def test_a_reorder_by_keys_resolves_them_in_one_query(app_engine) -> None:
    ctx = _ctx()
    todos = [await _create(ctx, f"t{i}") for i in range(10)]
    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    async def reorder(refs: list[str]) -> int:
        async with session_for(ctx) as conn:
            statements.clear()
            event.listen(app_engine.sync_engine, "before_cursor_execute", count)
            try:
                await store.reorder_todos(
                    conn, ctx, board_id=todos[0].board_id, status="open", todo_ids=refs
                )
            finally:
                event.remove(app_engine.sync_engine, "before_cursor_execute", count)
        return len(statements)

    by_id = await reorder([t.id for t in reversed(todos)])
    assert await reorder([t.key for t in reversed(todos)]) == by_id + 1


async def test_a_reorder_by_keys_takes_a_thousand_todo_lane(app_engine) -> None:
    ctx = _ctx()
    board = await _board(ctx, "Large")
    async with session_for(ctx) as conn:
        for _ in range(10):
            await store.plan_todos(conn, ctx, todos=[{"title": "t"}] * 100, board_id=board.id)
    keys = [f"LAR-{n}" for n in range(1000, 0, -1)]
    async with session_for(ctx) as conn:
        n = await store.reorder_todos(conn, ctx, board_id=board.id, status="open", todo_ids=keys)
    assert n == 1000


async def test_an_unknown_key_is_not_found_as_an_unknown_id_is(app_engine) -> None:
    ctx = _ctx()
    todo = await _create(ctx, "a")
    async with session_for(ctx) as conn:
        for ref in ["TODO-2", "NOPE-1"]:
            with pytest.raises(TodoNotFoundError) as caught:
                await store.get_todo(conn, ctx, todo_id=ref)
            assert str(caught.value) == ref
        with pytest.raises(TodoNotFoundError, match="TODO-9"):
            await store.reorder_todos(
                conn, ctx, board_id=todo.board_id, status="open", todo_ids=["TODO-9"]
            )
        with pytest.raises(TodoNotFoundError):
            await store.create_todo(conn, ctx, title="b", parent_id="TODO-9")
        page, _ = await store.list_todos(conn, ctx, parent_id="TODO-9")
        assert page == []


async def test_a_board_key_resolves_in_the_query_an_id_would_take(app_engine) -> None:
    ctx = _ctx()
    board = await _board(ctx, "Magnificent")
    todos = [await _create(ctx, f"t{i}", board_id=board.id) for i in range(5)]
    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    async def counted(call) -> int:
        async with session_for(ctx) as conn:
            statements.clear()
            event.listen(app_engine.sync_engine, "before_cursor_execute", count)
            try:
                await call(conn)
            finally:
                event.remove(app_engine.sync_engine, "before_cursor_execute", count)
        return len(statements)

    keys = [t.key for t in reversed(todos)]
    for call in [
        lambda conn, ref: store.reorder_todos(
            conn, ctx, board_id=ref, status="open", todo_ids=keys
        ),
        lambda conn, ref: store.list_todos(conn, ctx, board_id=ref),
        lambda conn, ref: store.get_board_summary(conn, ctx, board_id=ref),
        lambda conn, ref: store.update_board(conn, ctx, board_id=ref, name="M"),
    ]:
        by_id = await counted(lambda conn, call=call: call(conn, board.id))
        assert await counted(lambda conn, call=call: call(conn, "mag")) == by_id


async def test_a_board_key_names_a_board_only_in_its_own_workspace(app_engine) -> None:
    ctx = _ctx()
    await _board(ctx, "Magnificent")
    elsewhere = make_ctx(ctx.tenant_id, workspace_id="ws_2")
    async with session_for(elsewhere) as conn:
        with pytest.raises(store.BoardNotFoundError) as caught:
            await store.get_board_summary(conn, elsewhere, board_id="MAG")
        assert "MAG" in str(caught.value)
