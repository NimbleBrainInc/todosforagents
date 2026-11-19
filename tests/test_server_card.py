"""The Server Card a host reads before it connects, at ``<mcp-path>/server-card``.

The card is built from the server itself (``FastMCP(...)`` and ``add_server_card(...)``), so it
names and titles the server as ``serverInfo`` does, with the package's version, and places the
board UI the server lists.
"""

from __future__ import annotations

from importlib.metadata import version

import httpx2
from fastmcp import Client
from nimblebrain_server_sdk import HOST_META_KEY, SERVER_CARD_MEDIA_TYPE

from mcp_todos import server


async def served_card() -> dict:
    """Fetch the card the way a host does, from the streamable-HTTP app, in process."""
    transport = httpx2.ASGITransport(app=server.mcp.http_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        response = await http.get("/mcp/server-card", headers={"accept": SERVER_CARD_MEDIA_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == SERVER_CARD_MEDIA_TYPE
    return response.json()


async def test_the_card_names_the_server_as_server_info_does() -> None:
    card = await served_card()
    async with Client(server.mcp) as client:
        info = client.server_info
    assert info is not None
    assert (card["name"], card["version"], card["title"]) == (info.name, info.version, info.title)
    assert card["name"] == "com.todosforagents/todos"
    # Without version=, FastMCP would report its own version as the server's.
    assert card["version"] == version(server.DISTRIBUTION)
    assert card["title"] == "Todos for Agents"
    assert 1 <= len(card["description"]) <= 100
    assert card["icons"][0]["src"].startswith("data:image/png;base64,")


async def test_the_card_places_the_board_the_server_lists() -> None:
    card = await served_card()
    placements = card["_meta"][HOST_META_KEY]["placements"]
    assert placements == [
        {
            "slot": "sidebar.apps",
            "resourceUri": "ui://todos/main",
            "route": "@nimblebraininc/todos",
            "label": "To-dos",
        }
    ]
    listed = {str(r.uri) for r in await server.mcp.list_resources()}
    assert placements[0]["resourceUri"] in listed
