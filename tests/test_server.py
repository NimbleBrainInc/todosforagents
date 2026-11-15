"""The tool layer end to end: every tool's documented shape, run in this process.

The wiring that needs no store body:

  * all 17 tools are registered, each invocable as a plain async function;
  * identity is **fail-closed**: when the runtime can establish no identity, every tool
    returns the structured ``unauthenticated`` result (with a non-empty ``next_step``)
    BEFORE any database session is opened;
  * no return on that path leaks ``tenant_id`` / ``workspace_id``.

The behavioral cases run the real tool bodies on the local runtime's SQLite file: the write
and board tools (create / plan / update / transitions / reorder / archive, board
create/update/list) and the read tools (list / get / next / summary / search) return their
documented shape with a non-empty ``next_step``.
"""

from __future__ import annotations

import json

import pytest
from nimblebrain_server_sdk import PREVIEW_CHARS
from tool_errors import error_of

import mcp_todos.server as server
from mcp_todos.models import PlanItem
from mcp_todos.runtime import AuthError

# Every tool the surface defines — the registered FastMCP tool callables.
_TOOL_NAMES = [
    "create_todo",
    "plan_todos",
    "update_todo",
    "start_todo",
    "complete_todo",
    "block_todo",
    "reopen_todo",
    "cancel_todo",
    "reorder_todos",
    "archive_todo",
    "list_todos",
    "get_todo",
    "get_next_todo",
    "get_board_summary",
    "list_boards",
    "create_board",
    "update_board",
]


@pytest.fixture
def no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installed runtime establishes no identity for any call."""

    def refuse() -> None:
        raise AuthError("no identity")

    monkeypatch.setattr(server.runtime(), "identity", refuse)


def _assert_no_identity_leak(payload: object) -> None:
    """No wire return may contain tenant_id / workspace_id anywhere."""
    blob = json.dumps(payload, default=str)
    assert "tenant_id" not in blob, f"tenant_id leaked: {blob}"
    assert "workspace_id" not in blob, f"workspace_id leaked: {blob}"


def test_full_tool_surface_is_registered():
    """All 17 tools are defined as invocable async callables on the server."""
    for name in _TOOL_NAMES:
        fn = getattr(server, name, None)
        assert fn is not None, f"missing tool: {name}"
        assert callable(fn), f"tool not callable: {name}"


def test_app_constructs_and_exposes_probes():
    """The ASGI app constructs (the runtime wrapped it) and the server object exists."""
    assert server.app is not None
    assert server.mcp is not None
    assert server.SERVICE == "todos"


@pytest.mark.asyncio
async def test_missing_identity_fails_closed_with_next_step(no_identity):
    """Every write/read tool returns the structured ``unauthenticated`` result
    (non-empty ``next_step``, no identity leak) when the runtime establishes no identity,
    fail-closed BEFORE any DB session is opened."""
    results = [
        await server.create_todo(title="x"),
        await server.plan_todos(todos=[PlanItem(title="x")]),
        await server.update_todo(todo_id="td_x"),
        await server.start_todo(todo_id="td_x"),
        await server.complete_todo(todo_id="td_x"),
        await server.block_todo(todo_id="td_x", reason="r"),
        await server.reopen_todo(todo_id="td_x"),
        await server.cancel_todo(todo_id="td_x"),
        await server.reorder_todos(board_id="bd_x", status="open", todo_ids=[]),
        await server.archive_todo(todo_id="td_x"),
        await server.list_todos(),
        await server.get_todo(todo_id="td_x"),
        await server.get_next_todo(),
        await server.get_board_summary(),
        await server.list_boards(),
        await server.create_board(name="b"),
        await server.update_board(board_id="bd_x"),
    ]
    for res in results:
        err = error_of(res)
        assert err["code"] == "unauthenticated"
        assert err["next_step"].strip()
        _assert_no_identity_leak(err)


# --------------------------------------------------------------------------- #
# The real store bodies                                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def headers(app_engine) -> None:
    """This test's database, live behind the installed runtime."""


@pytest.mark.asyncio
async def test_create_todo_tool_returns_shape_and_next_step(headers):
    """create_todo returns {todo, action, next_step}; no identity leak; the to-do is
    on the auto-created default board."""
    res = await server.create_todo(title="Write the report")
    assert res["action"] == "created"
    assert res["todo"]["title"] == "Write the report"
    assert res["todo"]["status"] == "open"
    assert res["todo"]["board_id"].startswith("bd_")
    assert res["next_step"].strip()
    _assert_no_identity_leak(res)


@pytest.mark.asyncio
async def test_create_todo_idempotent_tool(headers):
    """A retried create_todo with the same idempotency_key returns action=matched
    and the same to-do id (no duplicate)."""
    first = await server.create_todo(title="Once", idempotency_key="k1")
    second = await server.create_todo(title="Once again", idempotency_key="k1")
    assert first["action"] == "created"
    assert second["action"] == "matched"
    assert second["todo"]["id"] == first["todo"]["id"]


@pytest.mark.asyncio
async def test_plan_todos_tool_returns_created_count(headers):
    """plan_todos returns {todos, created, next_step} for a multi-step plan."""
    res = await server.plan_todos(todos=[PlanItem(title="A"), PlanItem(title="B")])
    assert res["created"] == 2
    assert [t["title"] for t in res["todos"]] == ["A", "B"]
    assert res["next_step"].strip()
    _assert_no_identity_leak(res)


@pytest.mark.asyncio
async def test_transition_tools_report_previous_status(headers):
    """The transition verbs return {todo, previous_status, next_step} with the
    expected status + side effects."""
    created = await server.create_todo(title="Flow")
    tid = created["todo"]["id"]

    started = await server.start_todo(todo_id=tid)
    assert started["todo"]["status"] == "in_progress"
    assert started["previous_status"] == "open"

    blocked = await server.block_todo(todo_id=tid, reason="waiting on review")
    assert blocked["todo"]["status"] == "blocked"
    assert blocked["todo"]["blocked_reason"] == "waiting on review"
    assert blocked["previous_status"] == "in_progress"

    completed = await server.complete_todo(todo_id=tid)
    assert completed["todo"]["status"] == "done"
    assert completed["todo"]["completed_at"]
    assert completed["previous_status"] == "blocked"

    reopened = await server.reopen_todo(todo_id=tid)
    assert reopened["todo"]["status"] == "open"
    assert reopened["previous_status"] == "done"

    cancelled = await server.cancel_todo(todo_id=tid)
    assert cancelled["todo"]["status"] == "cancelled"
    assert cancelled["previous_status"] == "open"


@pytest.mark.asyncio
async def test_update_todo_tool_is_partial(headers):
    """update_todo returns {todo, next_step} and touches only provided fields."""
    created = await server.create_todo(title="Edit me", details="keep me")
    tid = created["todo"]["id"]
    res = await server.update_todo(todo_id=tid, title="Edited")
    assert res["todo"]["title"] == "Edited"
    assert res["todo"]["details"] == "keep me"  # untouched
    assert res["next_step"].strip()


@pytest.mark.asyncio
async def test_archive_todo_tool(headers):
    """archive_todo returns {todo, next_step} with archived_at set."""
    created = await server.create_todo(title="Archive")
    res = await server.archive_todo(todo_id=created["todo"]["id"])
    assert res["todo"]["archived_at"]
    assert res["next_step"].strip()


@pytest.mark.asyncio
async def test_reorder_todos_tool(headers):
    """reorder_todos returns {ordered, next_step} with the count reordered."""
    a = await server.create_todo(title="A")
    b = await server.create_todo(title="B")
    board_id = a["todo"]["board_id"]
    res = await server.reorder_todos(
        board_id=board_id, status="open", todo_ids=[b["todo"]["id"], a["todo"]["id"]]
    )
    assert res["ordered"] == 2
    assert res["next_step"].strip()


@pytest.mark.asyncio
async def test_board_tools(headers):
    """create_board / update_board / list_boards return their documented shapes."""
    created = await server.create_board(name="My board")
    assert created["board"]["name"] == "My board"
    assert created["board"]["is_default"] is False
    board_id = created["board"]["id"]

    updated = await server.update_board(board_id=board_id, name="Renamed")
    assert updated["board"]["name"] == "Renamed"

    listing = await server.list_boards()
    names = [b["name"] for b in listing["boards"]]
    assert "Renamed" in names
    assert listing["next_step"].strip()


@pytest.mark.asyncio
async def test_unknown_todo_id_yields_not_found(headers):
    """A transition on an unknown to-do id returns the structured not_found dict
    (404 semantics), never a crash."""
    res = await server.start_todo(todo_id="td_missing")
    err = error_of(res)
    assert err["code"] == "not_found"
    assert err["entity"] == "todo"
    assert err["next_step"].strip()
    _assert_no_identity_leak(err)


@pytest.mark.asyncio
async def test_list_todos_tool_returns_page_and_cursor(headers):
    """list_todos returns {todos, next_cursor, next_step}; cursor pages cleanly."""
    for i in range(3):
        await server.create_todo(title=f"T{i}")

    res = await server.list_todos(limit=2)
    assert len(res["todos"]) == 2
    assert res["next_cursor"]  # a full page → a cursor
    assert res["next_step"].strip()
    _assert_no_identity_leak(res)

    page2 = await server.list_todos(limit=2, cursor=res["next_cursor"])
    assert len(page2["todos"]) == 1
    assert page2["next_cursor"] is None
    # No overlap between the two pages.
    ids1 = {t["id"] for t in res["todos"]}
    ids2 = {t["id"] for t in page2["todos"]}
    assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_get_todo_tool_returns_composite(headers):
    """get_todo returns {todo, steps, counts, recent_activity, next_step}."""
    goal = await server.create_todo(title="Goal")
    gid = goal["todo"]["id"]
    await server.create_todo(title="Sub", parent_id=gid)

    res = await server.get_todo(todo_id=gid)
    assert res["todo"]["id"] == gid
    assert len(res["steps"]) == 1
    assert res["counts"]["steps"] == 1
    assert isinstance(res["recent_activity"], list)
    assert res["next_step"].strip()
    _assert_no_identity_leak(res)


@pytest.mark.asyncio
async def test_get_todo_tool_unknown_is_not_found(headers):
    """get_todo on an unknown id returns the structured not_found dict."""
    res = await server.get_todo(todo_id="td_missing")
    err = error_of(res)
    assert err["code"] == "not_found"
    assert err["entity"] == "todo"
    assert err["next_step"].strip()


@pytest.mark.asyncio
async def test_get_next_todo_tool(headers):
    """get_next_todo returns the highest-priority actionable to-do, or null when none."""
    # Nothing actionable yet → to-do is None.
    empty = await server.get_next_todo()
    assert empty["todo"] is None
    assert empty["next_step"].strip()

    await server.create_todo(title="Low", priority="low")
    urgent = await server.create_todo(title="Urgent", priority="urgent")
    res = await server.get_next_todo()
    assert res["todo"]["id"] == urgent["todo"]["id"]
    assert res["next_step"].strip()
    _assert_no_identity_leak(res)


@pytest.mark.asyncio
async def test_get_board_summary_tool(headers):
    """get_board_summary returns {board, lanes, totals, attention, next_step}."""
    await server.create_todo(title="One")
    res = await server.get_board_summary()
    assert res["board"]["is_default"] is True
    assert isinstance(res["lanes"], list)
    assert res["totals"]["active"] == 1
    assert "overdue" in res["attention"]
    assert res["next_step"].strip()
    _assert_no_identity_leak(res)


@pytest.mark.asyncio
async def test_get_board_summary_tool_no_default_is_not_found(headers):
    """get_board_summary with no default board (and no board_id) returns not_found —
    a read never auto-creates a board."""
    res = await server.get_board_summary()
    err = error_of(res)
    assert err["code"] == "not_found"
    assert err["entity"] == "board"
    assert err["next_step"].strip()


@pytest.mark.asyncio
async def test_list_todos_query(headers):
    """list_todos(query=...) returns {todos, next_cursor, next_step} matching all query
    tokens; a query that matches nothing says so."""
    await server.create_todo(title="Deploy the cluster", details="kubernetes")
    await server.create_todo(title="Write docs")

    res = await server.list_todos(query="deploy kubernetes")
    assert [t["title"] for t in res["todos"]] == ["Deploy the cluster"]
    assert res["next_cursor"] is None
    assert res["next_step"].strip()
    _assert_no_identity_leak(res)

    empty = await server.list_todos(query="nothing-here")
    assert empty["todos"] == []
    assert empty["next_step"].startswith("No match.")


@pytest.mark.asyncio
async def test_list_todos_query_pages_by_its_own_cursor(headers):
    """A query's next_cursor resumes the query; the plain listing refuses it."""
    for i in range(3):
        await server.create_todo(title=f"rollout {i}")
    await server.create_todo(title="unrelated")

    first = await server.list_todos(query="rollout", limit=2)
    assert len(first["todos"]) == 2 and first["next_cursor"]
    rest = await server.list_todos(query="rollout", limit=2, cursor=first["next_cursor"])
    seen = [t["title"] for t in first["todos"] + rest["todos"]]
    assert sorted(seen) == ["rollout 0", "rollout 1", "rollout 2"]
    assert rest["next_cursor"] is None

    refused = await server.list_todos(cursor=first["next_cursor"])
    assert error_of(refused)["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# Page rows are bounded: a preview of details, the whole to-do from get_todo    #
# --------------------------------------------------------------------------- #

# assert_bounded_rows' default ceiling; the seeded details run past it, so a details
# leak under any name fails.
_ROW_CEILING = 1_000
_LONG = " ".join(["rollout checklist item"] * 200)


def _texts(value: object) -> list[str]:
    """Every string in a row, nested values included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


def _assert_bounded(rows: list[dict]) -> None:
    """``assert_bounded_rows`` without its ``uri`` check: To-dos serves no per-record
    resource, so a row is addressed by the ``id`` get_todo reads."""
    assert rows, "an empty page proves nothing about its rows"
    for row in rows:
        assert row["id"].startswith("td_")
        assert "details" not in row, f"{row['id']} carries its details"
        preview = row["details_preview"]
        assert len(preview) <= PREVIEW_CHARS
        assert preview.endswith("…") and _LONG.startswith(preview[:-1])
        for text in _texts(row):
            assert len(text) <= _ROW_CEILING, f"{row['id']} holds {len(text)} characters"


@pytest.mark.asyncio
async def test_page_rows_carry_a_bounded_details_preview(headers):
    """A page runs to 200 rows of up to 64 KiB of details each, so list, search and
    plan rows carry ``details_preview`` and leave the details to get_todo."""
    assert len(_LONG) > _ROW_CEILING
    planned = await server.plan_todos(todos=[PlanItem(title="Roll out", details=_LONG)])

    pages = {
        "plan": planned["todos"],
        "list": (listed := await server.list_todos())["todos"],
        "search": (searched := await server.list_todos(query="rollout"))["todos"],
    }
    for name, rows in pages.items():
        _assert_bounded(rows)
        assert len(rows) == 1, name
    for res in (planned, listed, searched):
        assert "get_todo(todo_id=...)" in res["next_step"]

    whole = await server.get_todo(todo_id=planned["todos"][0]["id"])
    assert whole["todo"]["details"] == _LONG


@pytest.mark.asyncio
async def test_get_todo_steps_are_bounded_rows(headers):
    """A goal's steps are rows: each carries its id and a details preview, and its whole
    record is its own get_todo."""
    goal = await server.create_todo(title="Goal", details=_LONG)
    gid = goal["todo"]["id"]
    await server.plan_todos(
        todos=[PlanItem(title="Step A", details=_LONG), PlanItem(title="Step B", details=_LONG)],
        parent_id=gid,
    )

    res = await server.get_todo(todo_id=gid)
    assert res["todo"]["details"] == _LONG, "the to-do read comes whole"
    _assert_bounded(res["steps"])
    assert len(res["steps"]) == 2
    assert "get_todo(todo_id=<the step's id>)" in res["next_step"]

    step = await server.get_todo(todo_id=res["steps"][0]["id"])
    assert step["todo"]["details"] == _LONG


@pytest.mark.asyncio
async def test_short_details_preview_as_themselves(headers):
    """A preview cuts only what is over the length; a to-do without details has none."""
    await server.create_todo(title="Short", details="ship it")
    await server.create_todo(title="Bare")
    rows = {r["title"]: r for r in (await server.list_todos())["todos"]}
    assert rows["Short"]["details_preview"] == "ship it"
    assert "details_preview" not in rows["Bare"] and "details" not in rows["Bare"]
