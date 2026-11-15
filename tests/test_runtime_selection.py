"""Runtime selection refuses rather than guesses.

``TODOS_RUNTIME`` unset, or naming no installed runtime, refuses to start: through
:func:`select_runtime`, and through the generic stdio entry point (``python -m mcp_todos``) as
a non-zero exit. A multi-principal deployment that fell back to a single-principal runtime
would drop its isolation, so nothing infers one. ``todosforagents`` is the local runtime's own
command, so it selects that runtime explicitly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import EntryPoint, entry_points

import pytest

from mcp_todos import server
from mcp_todos.runtime import ENTRY_POINT_GROUP, RuntimeSelectionError, select_runtime
from mcp_todos_local.runtime import LocalRuntime


def _env(value: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "TODOS_RUNTIME"}
    if value is not None:
        env["TODOS_RUNTIME"] = value
    return env


def test_the_local_runtime_registers_under_the_entry_point_group() -> None:
    assert {ep.name for ep in entry_points(group=ENTRY_POINT_GROUP)} == {"local"}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_refuses(value: str | None) -> None:
    env = {} if value is None else {"TODOS_RUNTIME": value}
    with pytest.raises(RuntimeSelectionError, match="not set"):
        select_runtime(env)


@pytest.mark.parametrize("value", ["cloud", "Local", "postgres"])
def test_a_name_no_installed_runtime_registers_refuses(value: str) -> None:
    with pytest.raises(RuntimeSelectionError, match="names no installed runtime"):
        select_runtime({"TODOS_RUNTIME": value})


def test_a_runtime_that_is_not_installed_refuses() -> None:
    nothing: list[EntryPoint] = []
    with pytest.raises(RuntimeSelectionError, match=r"installed: none"):
        select_runtime({"TODOS_RUNTIME": "local"}, available=nothing)


def test_an_installed_runtime_is_selected_by_name() -> None:
    assert isinstance(select_runtime({"TODOS_RUNTIME": "local"}), LocalRuntime)


def test_a_process_serves_one_runtime() -> None:
    # The suite installed a local runtime; a second one is refused, not swapped in.
    installed = server.runtime()
    with pytest.raises(RuntimeError, match="already installed"):
        server.install(LocalRuntime())
    assert server.runtime() is installed


@pytest.mark.parametrize("value", [None, "", "cloud"])
def test_the_generic_stdio_entry_point_exits_non_zero(value: str | None) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "mcp_todos"],
        env=_env(value),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "TODOS_RUNTIME" in done.stderr
    assert done.stdout == ""


def test_the_local_command_refuses_another_runtime() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "mcp_todos_local"],
        env=_env("cloud"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "TODOS_RUNTIME" in done.stderr
    assert done.stdout == ""
