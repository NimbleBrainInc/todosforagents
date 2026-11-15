"""An installed wheel serves the built UI with no setting and no source checkout.

The wheel carries ``ui/dist/index.html`` as ``mcp_todos/ui_dist/index.html``
(``hatch_build.py``). This builds the wheel, unpacks it as an install would, and loads the UI
from it in a fresh process with ``TODOS_UI_DIR`` unset and the source tree out of reach.

It needs the UI built (``make ui-build``). Without it the test skips locally and fails in CI,
where a skip would report green having proved nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BUILT_UI = REPO / "ui" / "dist" / "index.html"

PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import mcp_todos.ui as ui
print(json.dumps({"file": ui.__file__, "dir": str(ui._UI_DIR), "html": ui.load_ui()}))
"""


def test_an_installed_wheel_serves_the_built_ui(tmp_path: Path) -> None:
    if not BUILT_UI.is_file():
        message = "the UI is not built; run `make ui-build`"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    dist, site, cwd = tmp_path / "dist", tmp_path / "site", tmp_path / "elsewhere"
    cwd.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=REPO,
        capture_output=True,
        check=True,
        timeout=300,
    )
    (wheel,) = dist.glob("todosforagents-*.whl")
    zipfile.ZipFile(wheel).extractall(site)

    env = {k: v for k, v in os.environ.items() if k != "TODOS_UI_DIR"}
    done = subprocess.run(
        [sys.executable, "-c", PROBE, str(site)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    loaded = json.loads(done.stdout)
    assert Path(loaded["file"]).is_relative_to(site), "the probe imported the source tree"
    assert Path(loaded["dir"]) == site / "mcp_todos" / "ui_dist"
    assert loaded["html"] == BUILT_UI.read_text(encoding="utf-8")
    assert "not built" not in loaded["html"].lower()
