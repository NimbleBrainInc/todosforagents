"""The notification that says to-do or board data changed.

Driven through a real MCP client connected in memory, on the local runtime's SQLite file,
so what is asserted is what the server sends (how it travels over HTTP
is ``nimblebrain-server-sdk``'s test): every write
announces ``notifications/resources/list_changed`` exactly once on the session
that made it, and a write that failed or changed nothing announces nothing.

A read of any kind announces nothing, including a read that fails and writes a
denied/error audit row. That is the loop guard: a view re-reads when told data
changed, so a read that announced would make every view refresh itself forever.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import mcp.types
import pytest
from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from tool_errors import error_of

from mcp_todos.server import mcp as server

SEED_KEY = "seed-key"


class Announcements(MessageHandler):
    """Every ``notifications/resources/list_changed`` the client received."""

    def __init__(self) -> None:
        self.received: list[mcp.types.ResourceListChangedNotification] = []

    async def on_resource_list_changed(
        self, message: mcp.types.ResourceListChangedNotification
    ) -> None:
        self.received.append(message)


@pytest.fixture
def caller(app_engine) -> None:
    """This test's database, live behind the installed runtime."""


@pytest.fixture
def announcements() -> Announcements:
    return Announcements()


async def call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def fail(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call ``tool`` expecting an error result; return its error."""
    return error_of(await client.call_tool(tool, args, raise_on_error=False))


async def seed(client: Client) -> dict[str, str]:
    """A to-do on the default board and a second board, for writes to act on."""
    created = await call(client, "create_todo", {"title": "Seed", "idempotency_key": SEED_KEY})
    board = await call(client, "create_board", {"name": "Seed board"})
    return {
        "todo_id": created["todo"]["id"],
        "default_board_id": created["todo"]["board_id"],
        "board_id": board["board"]["id"],
    }


WRITES: list[tuple[str, Callable[[dict[str, str]], dict[str, Any]]]] = [
    ("create_todo", lambda s: {"title": "New"}),
    ("plan_todos", lambda s: {"todos": [{"title": "A"}, {"title": "B"}, {"title": "C"}]}),
    ("update_todo", lambda s: {"todo_id": s["todo_id"], "title": "Renamed"}),
    ("start_todo", lambda s: {"todo_id": s["todo_id"]}),
    ("complete_todo", lambda s: {"todo_id": s["todo_id"]}),
    ("block_todo", lambda s: {"todo_id": s["todo_id"], "reason": "waiting"}),
    ("reopen_todo", lambda s: {"todo_id": s["todo_id"]}),
    ("cancel_todo", lambda s: {"todo_id": s["todo_id"]}),
    (
        "reorder_todos",
        lambda s: {"board_id": s["default_board_id"], "status": "open", "todo_ids": [s["todo_id"]]},
    ),
    ("archive_todo", lambda s: {"todo_id": s["todo_id"]}),
    ("create_board", lambda s: {"name": "Another"}),
    ("update_board", lambda s: {"board_id": s["board_id"], "name": "Renamed board"}),
]


@pytest.mark.parametrize(("tool", "args"), WRITES, ids=[tool for tool, _ in WRITES])
async def test_each_write_announces_once(caller, announcements, tool, args):
    async with Client(server, message_handler=announcements) as client:
        seeded = await seed(client)
        announcements.received.clear()

        result = await call(client, tool, args(seeded))

        assert "error" not in result
        assert len(announcements.received) == 1


async def test_an_idempotent_replay_announces_nothing(caller, announcements):
    async with Client(server, message_handler=announcements) as client:
        seeded = await seed(client)
        announcements.received.clear()

        replay = await call(client, "create_todo", {"title": "Seed", "idempotency_key": SEED_KEY})

        assert replay["action"] == "matched"
        assert replay["todo"]["id"] == seeded["todo_id"]
        assert announcements.received == []


async def test_a_status_advance_announces_on_every_step(caller, announcements):
    async with Client(server, message_handler=announcements) as client:
        todo_id = (await seed(client))["todo_id"]
        announcements.received.clear()

        await call(client, "start_todo", {"todo_id": todo_id})
        assert len(announcements.received) == 1

        await call(client, "complete_todo", {"todo_id": todo_id})
        assert len(announcements.received) == 2


READS: list[tuple[str, Callable[[dict[str, str]], dict[str, Any]]]] = [
    ("list_todos", lambda s: {}),
    ("get_todo", lambda s: {"todo_id": s["todo_id"]}),
    ("get_next_todo", lambda s: {}),
    ("get_board_summary", lambda s: {}),
    ("list_todos", lambda s: {"query": "seed"}),
    ("list_boards", lambda s: {}),
]


async def test_every_tool_is_classified_and_exactly_the_writers_announce():
    """A new tool fails here until it is annotated and placed in WRITES or READS.

    Under the spec's defaults a tool with no annotation reads as a destructive write, so
    every tool says whether it writes. The writers by annotation are exactly the tools the
    write cases prove announce, and the rest are exactly the tools the read cases prove
    stay silent.
    """
    tools = {t.name: t for t in await server.list_tools()}
    silent = sorted(
        n for n, t in tools.items() if t.annotations is None or t.annotations.read_only_hint is None
    )
    assert not silent, f"tools that do not say whether they write: {silent}"

    writes = {tool for tool, _ in WRITES}
    reads = {tool for tool, _ in READS}
    assert not writes & reads
    assert set(tools) == writes | reads
    hints = {n: t.annotations for n, t in tools.items() if t.annotations is not None}
    assert {n for n, a in hints.items() if not a.read_only_hint} == writes
    # A write that leaves `destructiveHint` unset reads, by the spec's default, as destructive.
    undecided = sorted(n for n in writes if hints[n].destructive_hint is None)
    assert not undecided, f"writes that do not say whether they destroy: {undecided}"


@pytest.mark.parametrize(("tool", "args"), READS, ids=[tool for tool, _ in READS])
async def test_a_read_announces_nothing(caller, announcements, tool, args):
    async with Client(server, message_handler=announcements) as client:
        seeded = await seed(client)
        announcements.received.clear()

        result = await call(client, tool, args(seeded))

        assert "error" not in result
        assert announcements.received == []


async def test_no_resource_read_or_failed_read_announces(caller, announcements):
    async with Client(server, message_handler=announcements) as client:
        await seed(client)
        announcements.received.clear()

        await client.list_resources()
        await client.read_resource("skill://todos/SKILL.md")

        # Failed reads write a denied/error audit row; that is still not view data.
        assert (await fail(client, "get_todo", {"todo_id": "td_missing"}))["code"] == "not_found"
        assert (await fail(client, "get_board_summary", {"board_id": "bd_missing"}))[
            "code"
        ] == "not_found"
        assert (await fail(client, "list_todos", {"cursor": "not-a-cursor"}))[
            "code"
        ] == "invalid_request"

    assert announcements.received == []


async def test_a_write_that_failed_announces_nothing(caller, announcements):
    async with Client(server, message_handler=announcements) as client:
        seeded = await seed(client)
        announcements.received.clear()

        failures = [
            await fail(client, "start_todo", {"todo_id": "td_missing"}),
            await fail(client, "update_todo", {"todo_id": "td_missing", "title": "x"}),
            await fail(client, "archive_todo", {"todo_id": "td_missing"}),
            await fail(client, "update_board", {"board_id": "bd_missing", "name": "x"}),
            await fail(
                client,
                "reorder_todos",
                {
                    "board_id": seeded["default_board_id"],
                    "status": "open",
                    "todo_ids": ["td_missing"],
                },
            ),
        ]

    assert [f["code"] for f in failures] == ["not_found"] * len(failures)
    assert announcements.received == []
