"""The MCP bundle in ``mcpb/`` runs the released package, at this version, on the local runtime.

The bundle carries no code: its ``pyproject.toml`` pins ``todosforagents`` from PyPI, and the
manifest starts it with ``uv run``. A release publishes the package and then the bundle, so
every version named here must be the package's own.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
BUNDLE_PROJECT = tomllib.loads((ROOT / "mcpb" / "pyproject.toml").read_text())
BUNDLE = BUNDLE_PROJECT["project"]
LOCK = tomllib.loads((ROOT / "uv.lock").read_text())
MANIFEST = json.loads((ROOT / "mcpb" / "manifest.json").read_text())


def test_the_bundle_names_the_package_version_everywhere() -> None:
    version = PACKAGE["version"]
    assert MANIFEST["version"] == version
    assert BUNDLE["version"] == version
    assert BUNDLE["dependencies"] == [f"{PACKAGE['name']}=={version}"]


def test_the_bundle_runs_the_local_runtime_with_uv_from_its_lock() -> None:
    server = MANIFEST["server"]
    assert server["type"] == "uv"
    config = server["mcp_config"]
    assert config["command"] == "uv"
    assert config["args"] == [
        "run",
        "--directory",
        "${__dirname}",
        "--frozen",
        "--no-dev",
        "python",
        "-m",
        "mcp_todos",
    ]
    assert config["env"] == {
        "TODOS_RUNTIME": "local",
        "TODOS_LOCAL_DB": "${user_config.database_path}",
    }
    assert set(MANIFEST["user_config"]) == {"database_path"}


def test_the_bundle_python_range_matches_its_project() -> None:
    assert MANIFEST["compatibility"]["runtimes"]["python"] == BUNDLE["requires-python"]
    assert PACKAGE["requires-python"] == ">=3.13"


def test_the_bundle_holds_the_sdk_to_the_reviewed_lock() -> None:
    sdk = "nimblebrain-server-sdk"
    (locked,) = [p["version"] for p in LOCK["package"] if p["name"] == sdk]
    assert BUNDLE_PROJECT["tool"]["uv"]["constraint-dependencies"] == [f"{sdk}=={locked}"]
