"""The MCPB manifest must publish exactly the tools the server registers.

``mcpb/manifest.json`` is the bundle's host-facing contract. The host discovers tools over the
wire and never gates on these lists, so a tool missing from one is
invisible to everything but a reader of the manifest. This pins each tool set; descriptions
are host-facing prose that legitimately differ from the docstrings, and stay a review concern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_todos.server import mcp

_ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (_ROOT / "mcpb" / "manifest.json",)


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: str(p.relative_to(_ROOT)))
async def test_manifest_publishes_exactly_the_registered_tools(manifest: Path) -> None:
    published = {t["name"] for t in json.loads(manifest.read_text())["tools"]}
    registered = {t.name for t in await mcp.list_tools()}
    assert registered, "the server registers no tools — the comparison would pass vacuously"
    assert published == registered, (
        f"{manifest.name} and the server disagree.\n"
        f"  registered but unpublished: {sorted(registered - published)}\n"
        f"  published but unregistered: {sorted(published - registered)}"
    )
