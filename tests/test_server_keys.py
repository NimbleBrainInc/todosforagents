"""Keys through the tools: every to-do id argument takes one, an unknown key is the not-found
an unknown id is, ``create_board`` reports the key it chose, and ``get_todo``
binds to the board UI."""

from __future__ import annotations

import pytest
from fastmcp import Client
from tool_errors import error_of

import mcp_todos.server as server
from mcp_todos.models import PlanItem

pytestmark = pytest.mark.asyncio


@pytest.fixture
def headers(app_engine) -> None:
    """This test's database, live behind the installed runtime."""


async def test_create_board_reports_the_key_it_chose(headers) -> None:
    res = await server.create_board(name="Magnificent board")
    assert res["board"]["key"] == "MAG"
    assert "MAG" in res["next_step"]
    explicit = await server.create_board(name="Anything", key="xy")
    assert explicit["board"]["key"] == "XY"


async def test_a_bad_or_taken_board_key_is_an_invalid_result(headers) -> None:
    await server.create_board(name="Launch")
    for key in ["lau", "X", "TODO"]:
        res = await server.create_board(name="Other", key=key)
        err = error_of(res)
        assert err["code"] == "invalid_request", key
        assert err["next_step"].strip()


async def test_every_todo_id_argument_takes_a_key(headers) -> None:
    board_id = (await server.create_board(name="Magnificent"))["board"]["id"]
    goal = (await server.create_todo(title="goal", board_id=board_id))["todo"]
    assert goal["key"] == "MAG-1"

    step = (await server.create_todo(title="step", board_id=board_id, parent_id="mag-1"))["todo"]
    assert step["key"] == "MAG-2" and step["parent_id"] == goal["id"]
    planned = await server.plan_todos(
        todos=[PlanItem(title="p")], board_id=board_id, parent_id="MAG-1"
    )
    assert planned["todos"][0]["key"] == "MAG-3"
    assert planned["todos"][0]["parent_id"] == goal["id"]

    got = await server.get_todo(todo_id="MAG-1")
    assert got["todo"]["id"] == goal["id"]
    assert {s["key"] for s in got["steps"]} == {"MAG-2", "MAG-3"}
    listed = await server.list_todos(parent_id="MAG-1")
    assert {t["key"] for t in listed["todos"]} == {"MAG-2", "MAG-3"}

    updated = await server.update_todo(todo_id="MAG-3", title="renamed", parent_id="MAG-2")
    assert updated["todo"]["parent_id"] == step["id"]

    for name, kwargs, status in [
        ("start_todo", {}, "in_progress"),
        ("block_todo", {"reason": "r"}, "blocked"),
        ("reopen_todo", {}, "open"),
        ("complete_todo", {}, "done"),
        ("cancel_todo", {}, "cancelled"),
    ]:
        moved = await getattr(server, name)(todo_id="mag-3", **kwargs)
        assert moved["todo"]["key"] == "MAG-3" and moved["todo"]["status"] == status, name

    reordered = await server.reorder_todos(
        board_id=board_id, status="open", todo_ids=["MAG-2", "MAG-1"]
    )
    assert reordered["ordered"] == 2
    archived = await server.archive_todo(todo_id="MAG-2")
    assert archived["todo"]["id"] == step["id"] and archived["todo"]["archived_at"]


async def test_rows_and_boards_carry_their_keys(headers) -> None:
    await server.create_todo(title="Find me")
    boards = await server.list_boards()
    assert [b["key"] for b in boards["boards"]] == ["TODO"]
    summary = await server.get_board_summary()
    assert summary["board"]["key"] == "TODO"
    assert (await server.get_next_todo())["todo"]["key"] == "TODO-1"
    assert (await server.list_todos(query="find"))["todos"][0]["key"] == "TODO-1"
    assert (await server.list_todos())["todos"][0]["key"] == "TODO-1"


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.get_todo(todo_id="TODO-7"),
        lambda: server.start_todo(todo_id="TODO-7"),
        lambda: server.update_todo(todo_id="TODO-7", title="x"),
        lambda: server.create_todo(title="x", parent_id="TODO-7"),
        lambda: server.archive_todo(todo_id="todo-7"),
    ],
)
async def test_an_unknown_key_is_not_found(headers, call) -> None:
    await server.create_todo(title="only one")
    res = await call()
    err = error_of(res)
    assert err["code"] == "not_found"
    assert err["entity"] == "todo"
    assert err["id"].upper() == "TODO-7"


async def test_get_todo_and_get_board_summary_bind_to_the_board_ui() -> None:
    async with Client(server.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in ("get_todo", "get_board_summary"):
        meta = tools[name].meta or {}
        assert meta["ui"] == {
            "resourceUri": server.BOARD_UI_BOUND_URI,
            "visibility": ["model", "app"],
        }


async def test_every_board_argument_takes_a_key(headers) -> None:
    board = (await server.create_board(name="Magnificent"))["board"]
    assert board["key"] == "MAG"

    created = (await server.create_todo(title="first", board_id="mag"))["todo"]
    assert created["board_id"] == board["id"] and created["key"] == "MAG-1"
    planned = await server.plan_todos(todos=[PlanItem(title="second")], board_id="Mag")
    assert planned["todos"][0]["board_id"] == board["id"]

    listed = await server.list_todos(board_id="MAG")
    assert {t["key"] for t in listed["todos"]} == {"MAG-1", "MAG-2"}
    assert (await server.get_next_todo(board_id="mag"))["todo"]["board_id"] == board["id"]
    found = await server.list_todos(query="first", board_id="MAG")
    assert [t["key"] for t in found["todos"]] == ["MAG-1"]
    assert (await server.get_board_summary(board_id="mag"))["board"]["id"] == board["id"]

    reordered = await server.reorder_todos(
        board_id="mag", status="open", todo_ids=["MAG-2", "MAG-1"]
    )
    assert reordered["ordered"] == 2
    assert [t["key"] for t in (await server.list_todos(board_id="MAG"))["todos"]] == [
        "MAG-2",
        "MAG-1",
    ]
    renamed = await server.update_board(board_id="mag", name="Renamed")
    assert renamed["board"]["id"] == board["id"] and renamed["board"]["name"] == "Renamed"


@pytest.mark.parametrize(
    "call",
    [
        lambda ref: server.create_todo(title="x", board_id=ref),
        lambda ref: server.plan_todos(todos=[PlanItem(title="x")], board_id=ref),
        lambda ref: server.reorder_todos(board_id=ref, status="open", todo_ids=[]),
        lambda ref: server.get_board_summary(board_id=ref),
        lambda ref: server.update_board(board_id=ref, name="x"),
    ],
)
async def test_an_unknown_board_key_is_not_found(headers, call) -> None:
    await server.create_board(name="Magnificent")
    unknown_id = await call("bd_unknown")
    assert error_of(unknown_id)["code"] == "not_found"
    assert error_of(unknown_id)["entity"] == "board"

    unknown_key = await call("nope")
    assert error_of(unknown_key)["code"] == "not_found"
    assert error_of(unknown_key)["entity"] == "board"
    assert error_of(unknown_key)["id"] == "nope"


async def test_a_board_filter_by_an_unknown_key_finds_nothing(headers) -> None:
    # A filter by an unknown id finds nothing rather than failing, and so does a key.
    board_id = (await server.create_board(name="Magnificent"))["board"]["id"]
    await server.create_todo(title="mine", board_id=board_id)
    assert (await server.list_todos(board_id="MAG"))["todos"]
    for ref in ("NOPE", "bd_unknown"):
        assert (await server.list_todos(board_id=ref))["todos"] == []
        assert (await server.get_next_todo(board_id=ref))["todo"] is None
        assert (await server.list_todos(query="mine", board_id=ref))["todos"] == []
