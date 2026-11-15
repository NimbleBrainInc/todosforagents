"""Check what the built wheel carries: the files read at runtime, and the built board UI.

Run on a directory holding one ``todosforagents`` wheel (``uv build --wheel``). The server reads
the skill, the icon, the local schema history and the built UI from inside its packages, so a
wheel without one of them installs a server that fails or serves a placeholder.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

#: The built UI inside the wheel (``hatch_build.py``).
UI_INDEX = "mcp_todos/ui_dist/index.html"

REQUIRED = {
    "mcp_todos/SKILL.md",
    "mcp_todos/icon.png",
    "mcp_todos/runtime.py",
    "mcp_todos/db/dialect.py",
    "mcp_todos_local/runtime.py",
    "mcp_todos_local/migrations/env.py",
    "mcp_todos_local/migrations/script.py.mako",
    "mcp_todos_local/migrations/versions/0001_initial.py",
    "mcp_todos_local/migrations/versions/0002_board_keys.py",
    UI_INDEX,
}
#: The only top-level directories a wheel may hold besides its ``.dist-info``.
PACKAGES = {"mcp_todos", "mcp_todos_local"}
ENTRY_POINTS = (
    "todosforagents = mcp_todos_local.__main__:main",
    "local = mcp_todos_local.runtime:LocalRuntime",
)


def check(wheel_path: Path) -> list[str]:
    wheel = zipfile.ZipFile(wheel_path)
    names = set(wheel.namelist())
    failures = [f"missing {f}" for f in sorted(REQUIRED - names)]
    if UI_INDEX in names and b"<html" not in wheel.read(UI_INDEX).lower():
        failures.append(f"{UI_INDEX} is not the built UI")
    tops = {n.split("/", 1)[0] for n in names if "/" in n and ".dist-info" not in n}
    failures += [f"carries {t}" for t in sorted(tops - PACKAGES)]
    [entry_points] = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
    declared = wheel.read(entry_points).decode()
    failures += [f"declares no entry point {e!r}" for e in ENTRY_POINTS if e not in declared]
    return [f"{wheel_path.name}: {f}" for f in failures]


def main(dist: str) -> int:
    wheels = sorted(Path(dist).glob("todosforagents-*.whl"))
    if len(wheels) != 1:
        print(f"expected one todosforagents-*.whl in {dist}, found {len(wheels)}", file=sys.stderr)
        return 1
    failures = check(wheels[0])
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print(f"{wheels[0].name}: OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist"))
