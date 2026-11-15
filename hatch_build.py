"""Put the built board UI inside the core's wheel, so every install serves it.

The UI is one inlined HTML file built from ``ui/`` (``cd ui && npm ci && npm run build``). A
wheel carries it as ``mcp_todos/ui_dist/index.html``, where :mod:`mcp_todos.ui` finds it with
no setting and no source checkout. Building a wheel without it fails, so no wheel ships the
"UI is not built" placeholder. An editable install reads ``ui/dist`` from the source tree
instead, so it needs no build hook.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

UI_INDEX = Path("ui") / "dist" / "index.html"
PACKAGED_AS = "mcp_todos/ui_dist/index.html"


class UIBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        index = Path(self.root) / UI_INDEX
        if not index.is_file():
            raise FileNotFoundError(
                f"{index} is missing: build the UI first (cd ui && npm ci && npm run build)"
            )
        build_data["force_include"][str(index)] = PACKAGED_AS
