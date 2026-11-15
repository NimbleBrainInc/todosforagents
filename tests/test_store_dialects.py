"""What must hold whatever the dialect does differently, checked on SQLite.

Timestamps: every timestamp the store returns is timezone-aware and in UTC. SQLite stores no
zone, so ``UTCDateTime`` stores UTC and re-attaches it; a naive value would make the keyset
cursor refuse the page, and a mis-shifted one would move a due date.

Paging over a timestamp sort: ``list_todos`` orders by ``(created_at DESC, id DESC)``. A plan
writes all its steps with one ``created_at``, so the walk crosses ties that only the id breaks,
and it must meet every to-do exactly once, in order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

from conftest import make_ctx, session_for

from mcp_todos.db import store
from mcp_todos.models import Todo


def _ctx():
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


def _is_utc(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() == timedelta(0)


async def test_every_returned_timestamp_is_utc_aware(app_engine) -> None:
    ctx = _ctx()
    # A due time given in another zone comes back as the same instant, in UTC.
    due = datetime(2030, 1, 2, 3, 4, 5, 678901, tzinfo=timezone(timedelta(hours=-10)))
    async with session_for(ctx) as conn:
        created, _ = await store.create_todo(conn, ctx, title="Due soon", due_at=due)
    async with session_for(ctx) as conn:
        done, _ = await store.complete_todo(conn, ctx, todo_id=created.id)
    async with session_for(ctx) as conn:
        archived = await store.archive_todo(conn, ctx, todo_id=created.id)
    async with session_for(ctx) as conn:
        todo, _steps, _counts, activity = await store.get_todo(conn, ctx, todo_id=created.id)
        [board] = await store.list_boards(conn, ctx)

    for t in (created, done, archived, todo):
        assert _is_utc(t.created_at) and _is_utc(t.updated_at)
        assert _is_utc(t.due_at)
    assert todo.due_at == due
    assert _is_utc(todo.completed_at) and _is_utc(todo.archived_at)
    assert _is_utc(board.created_at) and _is_utc(board.updated_at)
    assert activity and all(_is_utc(a.at) for a in activity)


async def test_a_naive_due_time_is_taken_as_utc(app_engine) -> None:
    ctx = _ctx()
    naive = datetime(2030, 6, 1, 12, 0, 0)
    async with session_for(ctx) as conn:
        created, _ = await store.create_todo(conn, ctx, title="Naive", due_at=naive)
    assert created.due_at == naive.replace(tzinfo=UTC)


async def test_a_timestamp_sorted_walk_meets_every_todo_once_in_order(app_engine) -> None:
    ctx = _ctx()
    async with session_for(ctx) as conn:
        # One plan: 13 steps sharing one created_at, so only the id orders them.
        planned, _ = await store.plan_todos(
            conn, ctx, todos=[{"title": f"step {i}"} for i in range(13)]
        )
    singles: list[Todo] = []
    for i in range(6):
        async with session_for(ctx) as conn:
            todo, _ = await store.create_todo(conn, ctx, title=f"single {i}")
        singles.append(todo)
    everything = planned + singles
    assert len({t.created_at for t in planned}) == 1

    seen: list[Todo] = []
    cursor: str | None = None
    while True:
        async with session_for(ctx) as conn:
            page, cursor = await store.list_todos(conn, ctx, limit=4, cursor=cursor)
        seen += page
        if cursor is None:
            break

    expected = sorted(everything, key=lambda t: (t.created_at, t.id), reverse=True)
    assert [t.id for t in seen] == [t.id for t in expected]


def _tier(query: str, title: str) -> int:
    """The match tier, computed apart from the store: exact title, title prefix, every token
    starting a title word, any other match."""
    t, q = title.lower(), query.lower()
    if t == q:
        return 4
    if t.startswith(q):
        return 3
    words = t.split()
    if all(any(w.startswith(tok) for w in words) for tok in q.split()):
        return 2
    return 1


async def test_a_query_walk_meets_every_match_once_in_tier_order(app_engine) -> None:
    ctx = _ctx()
    titles = ["Ship it", "ship IT now", "Now\tship it", "it ships", "reship it", "Unrelated"]
    async with session_for(ctx) as conn:
        # Plan steps share one created_at, so within a tier only the id orders them.
        planned, _ = await store.plan_todos(
            conn, ctx, todos=[{"title": t} for t in titles] + [{"title": "x", "details": "SHIP it"}]
        )
    singles: list[Todo] = []
    for t in titles:
        async with session_for(ctx) as conn:
            todo, _ = await store.create_todo(conn, ctx, title=t)
        singles.append(todo)
    matches = [t for t in planned + singles if t.title != "Unrelated"]

    seen: list[Todo] = []
    cursor: str | None = None
    while True:
        async with session_for(ctx) as conn:
            page, cursor = await store.list_todos(
                conn, ctx, query="ship it", limit=3, cursor=cursor
            )
        seen += page
        if cursor is None:
            break

    expected = sorted(
        matches, key=lambda t: (_tier("ship it", t.title), t.created_at, t.id), reverse=True
    )
    assert [t.id for t in seen] == [t.id for t in expected]
    assert [_tier("ship it", t.title) for t in seen][:2] == [4, 4]
