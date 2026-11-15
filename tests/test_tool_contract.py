"""The tool surface's contract with a model: errors, the status enum, create-time side
effects, annotations, and the edited fields an update records.

Errors are tool results flagged ``isError`` in one shape (``tool_errors.error_of`` holds every
error to it). ``status`` is an enum in each schema that takes one, so a client sees the set and
an unknown status is refused before the tool runs.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from tool_errors import error_of

import mcp_todos.server as server
from mcp_todos.db import store
from mcp_todos.models import STATUSES, PlanItem
from mcp_todos.runtime import AuthError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def headers(app_engine) -> None:
    """This test's database, live behind the installed runtime."""


async def _activity(todo_id: str) -> list[dict[str, Any]]:
    return (await server.get_todo(todo_id=todo_id))["recent_activity"]


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #


async def test_not_found_is_an_error_result(headers) -> None:
    err = error_of(await server.start_todo(todo_id="td_missing"))
    assert err["code"] == "not_found" and err["retryable"] is False
    assert err["entity"] == "todo" and err["id"] == "td_missing"


async def test_invalid_request_is_an_error_result(headers) -> None:
    err = error_of(await server.block_todo(todo_id="td_x", reason="   "))
    assert err["code"] == "invalid_request" and err["retryable"] is False


async def test_internal_error_is_an_error_result_without_the_fault(
    headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("secret connection string")

    monkeypatch.setattr(store, "list_boards", boom)
    err = error_of(await server.list_boards())
    assert err["code"] == "internal_error" and err["retryable"] is True
    assert "secret" not in str(err)


async def test_unauthenticated_is_an_error_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> None:
        raise AuthError("no identity")

    monkeypatch.setattr(server.runtime(), "identity", refuse)
    assert error_of(await server.list_boards())["code"] == "unauthenticated"


async def test_a_client_sees_the_error_flag_and_body(headers) -> None:
    async with Client(server.mcp) as client:
        res = await client.call_tool("get_todo", {"todo_id": "td_missing"}, raise_on_error=False)
    assert error_of(res)["code"] == "not_found"


# --------------------------------------------------------------------------- #
# The status enum                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("create_todo", {"title": "x", "status": "todo"}),
        ("plan_todos", {"todos": [{"title": "x", "status": "todo"}]}),
        ("list_todos", {"status": "todo"}),
        ("reorder_todos", {"board_id": "TODO", "status": "todo", "todo_ids": []}),
    ],
)
async def test_an_unknown_status_is_refused_by_the_schema(headers, tool, args) -> None:
    async with Client(server.mcp) as client:
        schema = {t.name: t.input_schema for t in await client.list_tools()}[tool]
        res = await client.call_tool(tool, args, raise_on_error=False)
    assert res.is_error is True
    # The schema rejected it: nothing ran, so the body is the validation message.
    assert res.structured_content is None
    assert _status_enums(schema) == [list(STATUSES)]


def _status_enums(schema: Any) -> list[list[str]]:
    """Every ``enum`` declared under a ``status`` property anywhere in ``schema``."""
    found: list[list[str]] = []

    def walk(node: Any, under_status: bool = False) -> None:
        if isinstance(node, dict):
            if under_status and "enum" in node:
                found.append(node["enum"])
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for prop, sub in value.items():
                        walk(sub, prop == "status")
                elif key == "$defs" and isinstance(value, dict):
                    for sub in value.values():
                        walk(sub)
                else:
                    walk(value, under_status)
        elif isinstance(node, list):
            for item in node:
                walk(item, under_status)

    walk(schema)
    return found


# --------------------------------------------------------------------------- #
# Create-time status honours its side effects                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_a_to_do_created_finished_has_completed_at(headers, status) -> None:
    todo = (await server.create_todo(title="x", status=status))["todo"]
    assert todo["status"] == status and todo["completed_at"]
    assert (await _activity(todo["id"]))[0]["detail"] == {"fields": ["completed_at"]}


async def test_a_to_do_created_blocked_needs_and_keeps_a_reason(headers) -> None:
    err = error_of(await server.create_todo(title="x", status="blocked"))
    assert err["code"] == "invalid_request" and "blocked_reason" in err["message"]

    todo = (
        await server.create_todo(title="x", status="blocked", blocked_reason="waiting on legal")
    )["todo"]
    assert todo["blocked_reason"] == "waiting on legal" and "completed_at" not in todo
    assert (await _activity(todo["id"]))[0]["detail"] == {"fields": ["blocked_reason"]}


async def test_a_reason_without_blocked_is_refused(headers) -> None:
    err = error_of(await server.create_todo(title="x", blocked_reason="why"))
    assert err["code"] == "invalid_request"


async def test_an_open_create_records_no_fields(headers) -> None:
    todo = (await server.create_todo(title="x"))["todo"]
    assert "completed_at" not in todo and "blocked_reason" not in todo
    assert "detail" not in (await _activity(todo["id"]))[0]


async def test_plan_items_honour_their_status(headers) -> None:
    planned = await server.plan_todos(
        todos=[
            PlanItem(title="a", status="done"),
            PlanItem(title="b", status="blocked", blocked_reason="waiting"),
            PlanItem(title="c"),
        ]
    )
    a, b, c = [(await server.get_todo(todo_id=t["id"]))["todo"] for t in planned["todos"]]
    assert a["completed_at"] and "blocked_reason" not in a
    assert b["blocked_reason"] == "waiting" and "completed_at" not in b
    assert "completed_at" not in c and "blocked_reason" not in c

    err = error_of(await server.plan_todos(todos=[PlanItem(title="x", status="blocked")]))
    assert err["code"] == "invalid_request" and "todos[0].blocked_reason" in err["message"]


# --------------------------------------------------------------------------- #
# Annotations                                                                  #
# --------------------------------------------------------------------------- #

_READS = ("list_todos", "get_todo", "get_next_todo", "get_board_summary", "list_boards")
# (readOnlyHint, destructiveHint, idempotentHint) per write.
_WRITES = {
    "create_todo": (False, False, False),
    "plan_todos": (False, False, False),
    "create_board": (False, False, False),
    "update_todo": (False, True, True),
    "update_board": (False, True, True),
    "reorder_todos": (False, True, True),
    "start_todo": (False, False, True),
    "block_todo": (False, False, True),
    "reopen_todo": (False, False, True),
    "complete_todo": (False, False, False),
    "cancel_todo": (False, False, False),
    "archive_todo": (False, True, False),
}


async def test_every_tool_is_annotated_closed_world() -> None:
    async with Client(server.mcp) as client:
        tools = {t.name: t.annotations for t in await client.list_tools()}
    assert set(tools) == set(_READS) | set(_WRITES)
    for name, hints in tools.items():
        assert hints is not None, name
        assert hints.open_world_hint is False, name
        if name in _READS:
            assert (hints.read_only_hint, hints.idempotent_hint) == (True, True), name
        else:
            got = (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint)
            assert got == _WRITES[name], name


# --------------------------------------------------------------------------- #
# Edits record the field names                                                 #
# --------------------------------------------------------------------------- #


async def test_an_update_records_the_fields_it_set_not_their_values(headers) -> None:
    todo = (await server.create_todo(title="x"))["todo"]
    await server.update_todo(todo_id=todo["id"], title="secret title", details="secret details")
    detail = (await _activity(todo["id"]))[0]["detail"]
    assert detail == {"fields": ["title", "details"]}

    await server.update_todo(todo_id=todo["id"], title="  ")
    assert (await _activity(todo["id"]))[0]["detail"] == {"fields": []}


async def test_a_board_update_records_the_fields_it_set(headers) -> None:
    board = (await server.create_board(name="Launch"))["board"]
    await server.update_board(board_id=board["id"], name="Launch plan", description="d")
    async with server.runtime().session(server.runtime().identity()) as conn:
        rows = (
            await conn.execute(
                store.select(store.todo_activity.c.detail).where(
                    store.todo_activity.c.todo_id == board["id"],
                    store.todo_activity.c.action == "update",
                )
            )
        ).all()
    assert [r[0] for r in rows] == [{"fields": ["name", "description"]}]
