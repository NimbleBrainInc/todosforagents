"""The ``get_started`` prompt, rendered the way a host gets it.

The golden surface freezes the prompt's listing (name, title, description, argument); this
locks what it renders. It is the person's way into the first-use flow, so it asks for the plan
in the terms the empty reads' ``next_step`` and the skill's "First use" section give the agent:
3 to 5 concrete to-dos, made with ``plan_todos``, told to the person by key.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_todos import server

PLAN = ("3 to 5 concrete", "plan_todos", "keys")


async def render(arguments: dict[str, str]) -> list[tuple[str, str]]:
    async with Client(server.mcp) as client:
        result = await client.get_prompt("get_started", arguments)
    return [(m.role, getattr(m.content, "text", "")) for m in result.messages]


@pytest.mark.asyncio
async def test_without_focus_it_checks_the_board_then_plans_or_reports_by_key() -> None:
    (role, text), *rest = await render({})
    assert role == "user" and not rest
    assert "get_board_summary" in text
    assert "ask me what I am working on" in text
    for phrase in PLAN:
        assert phrase in text
    assert "get_next_todo" in text
    assert "by its key" in text


@pytest.mark.asyncio
async def test_with_focus_it_plans_for_that() -> None:
    (role, text), *rest = await render({"focus": "  launching the beta  "})
    assert role == "user" and not rest
    assert "I am working on: launching the beta\n" in text
    for phrase in PLAN:
        assert phrase in text
    assert "get_board_summary" not in text


@pytest.mark.asyncio
async def test_a_blank_focus_is_no_focus() -> None:
    assert await render({"focus": "   "}) == await render({})


@pytest.mark.asyncio
async def test_it_asks_for_the_plan_the_empty_reads_give() -> None:
    (_, text), *_ = await render({})
    next_step = server._onboard(has_board=False)
    for phrase in ("propose 3 to 5 concrete to-dos", "plan_todos"):
        assert phrase in next_step
        assert phrase in text
