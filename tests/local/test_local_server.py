"""The local runtime end to end: all 17 tools over stdio and over loopback HTTP, and the
surface a client of a separate process sees held to the golden one.
"""

from __future__ import annotations

import json
from pathlib import Path

from local_harness import (
    GOLDEN_SURFACE,
    exercise_every_tool,
    http_client,
    http_server,
    normalize_ui_build,
    stdio_client,
    surface,
)

from mcp_todos import server

ALL_TOOLS = {t["name"] for t in json.loads(GOLDEN_SURFACE.read_text())["tools"]}


def test_the_scenario_covers_every_tool() -> None:
    assert len(ALL_TOOLS) == 17


async def test_every_tool_works_over_stdio(tmp_path: Path) -> None:
    async with stdio_client(tmp_path / "todos.db") as client:
        called = await exercise_every_tool(client)
    assert called == ALL_TOOLS


async def test_every_tool_works_over_http(tmp_path: Path) -> None:
    db = tmp_path / "todos.db"
    with http_server(db) as port:
        async with http_client(port, db) as client:
            called = await exercise_every_tool(client)
    assert called == ALL_TOOLS


async def test_stdio_and_http_share_one_database(tmp_path: Path) -> None:
    db = tmp_path / "todos.db"
    with http_server(db) as port:
        async with http_client(port, db) as client:
            await client.call_tool("create_todo", {"title": "From HTTP"})
        async with stdio_client(db) as client:
            result = await client.call_tool("list_todos", {})
    assert result.structured_content is not None
    assert [t["title"] for t in result.structured_content["todos"]] == ["From HTTP"]


async def test_a_stdio_client_sees_the_golden_surface(tmp_path: Path) -> None:
    golden = json.loads(GOLDEN_SURFACE.read_text())
    async with stdio_client(tmp_path / "todos.db") as client:
        local = json.loads(normalize_ui_build(json.dumps(await surface(client))))
    assert local == golden
    assert local["instructions"] == server.mcp.instructions
