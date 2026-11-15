"""Golden: the MCP surface this server advertises.

Every tool (name, title, description, input and output schemas, annotations, meta), every
resource and resource template (URI, name, description, MIME type, meta), the server's
capabilities and instructions, and a digest of the skill it serves. This is what agents and
hosts rely on, and the parity target for every runtime: a runtime may change where identity
comes from, never what a client sees.

``tests/golden/surface.json`` is frozen output with no regenerate path. A deliberate surface
change edits it in the same PR, where review sees the diff.

The board UI's bound URI carries a hash of the built HTML (``ui://todos/main/<12 hex>``), so it
changes with every UI build. The comparison replaces exactly that hex segment with ``<build>``;
everything else about the binding stays frozen.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from mcp_todos import server

GOLDEN = Path(__file__).parent / "golden" / "surface.json"

#: The build key in a bound board UI URI: the 12 hex characters after ``ui://todos/main/``.
_UI_BUILD = re.compile(r"(?<=ui://todos/main/)[0-9a-f]{12}(?![0-9a-f])")


def normalize_ui_build(text: str) -> str:
    """Replace each board UI build key in ``text`` with ``<build>``."""
    return _UI_BUILD.sub("<build>", text)


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


async def surface() -> dict[str, Any]:
    async with Client(server.mcp) as client:
        tools = sorted((_dump(t) for t in await client.list_tools()), key=lambda t: t["name"])
        resources = sorted(
            (_dump(r) for r in await client.list_resources()), key=lambda r: r["uri"]
        )
        templates = sorted(
            (_dump(t) for t in await client.list_resource_templates()),
            key=lambda t: t["uriTemplate"],
        )
        skill = await client.read_resource("skill://todos/SKILL.md")
        skill_text = "".join(getattr(part, "text", "") for part in skill)
        capabilities = _dump(client.server_capabilities) if client.server_capabilities else {}
    return {
        "instructions": server.mcp.instructions,
        "capabilities": capabilities,
        "tools": tools,
        "resources": resources,
        "resource_templates": templates,
        "skill_sha256": hashlib.sha256(skill_text.encode()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_surface_matches_golden() -> None:
    live = normalize_ui_build(json.dumps(await surface()))
    assert json.loads(live) == json.loads(GOLDEN.read_text())


def test_normalization_replaces_only_the_build_key() -> None:
    assert normalize_ui_build('"ui://todos/main/0123456789ab"') == '"ui://todos/main/<build>"'
    for untouched in (
        '"ui://todos/main"',
        '"ui://todos/main/{build}"',
        '"ui://todos/main/0123456789abc"',
        '"ui://todos/other/0123456789ab"',
    ):
        assert normalize_ui_build(untouched) == untouched
