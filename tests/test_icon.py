"""The server's mark reaches hosts by both routes: serverInfo and the bundle manifest."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_todos import server

ROOT = Path(__file__).resolve().parent.parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_server_info_carries_the_icon_as_an_inline_png() -> None:
    async with Client(server.mcp) as client:
        info = client.server_info
    assert info is not None and info.icons, "serverInfo carries no icon"
    icon = info.icons[0]
    assert icon.mime_type == "image/png"
    prefix = "data:image/png;base64,"
    assert icon.src.startswith(prefix)
    assert base64.b64decode(icon.src[len(prefix) :]).startswith(PNG_SIGNATURE)


@pytest.mark.parametrize("manifest", ["mcpb/manifest.json"])
def test_each_manifest_names_an_icon_file_beside_it(manifest: str) -> None:
    path = ROOT / manifest
    icon = path.parent / json.loads(path.read_text())["icon"]
    assert icon.read_bytes().startswith(PNG_SIGNATURE)
