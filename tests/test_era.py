"""The server serves both protocol eras: the sessionless 2026-07-28 era a v2 client
negotiates by default, and the session-based handshake a 2025-era host still speaks.

No database: listing and the ``skill://`` and ``ui://`` reads touch none (the engine is
lazy). Every resource read also passes FastMCP 4's templated-resource path screening, so a
URI the screening refused would fail here as resource-not-found.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_todos.server import BOARD_UI_BOUND_URI, mcp

SERVED = {
    "skill://todos/SKILL.md": "text/markdown",
    "ui://todos/main": "text/html;profile=mcp-app",
}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_both_eras_list_and_read_every_skill_and_ui_resource(mode: str):
    async with Client(mcp, mode=mode) as client:
        tools = await client.list_tools()
        resources = {str(r.uri): r for r in await client.list_resources()}
        reads = {uri: await client.read_resource(uri) for uri in SERVED}

    assert tools
    assert set(SERVED) <= set(resources)
    for uri, mime_type in SERVED.items():
        [content] = reads[uri]
        assert content.mime_type == mime_type
        assert getattr(content, "text", "")


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_both_eras_read_the_board_ui_under_a_build_key(mode: str):
    # The tools bind to a keyed URI that no listing carries; the template serves it.
    async with Client(mcp, mode=mode) as client:
        [content] = await client.read_resource(BOARD_UI_BOUND_URI)

    assert content.mime_type == "text/html;profile=mcp-app"
    assert getattr(content, "text", "")
