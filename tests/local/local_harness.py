"""Helpers for the local runtime's suites: start it the way an install does, and drive it.

Every server here is a separate process started with ``TODOS_RUNTIME=local``, over stdio
(``python -m mcp_todos``) or loopback HTTP (``python -m mcp_todos_local --http``, which is
``todosforagents --http``), so the
suites see exactly what a client of an installed server sees.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO = Path(__file__).resolve().parents[2]
GOLDEN_SURFACE = REPO / "tests" / "golden" / "surface.json"
#: The build key in a bound board UI URI: the 12 hex characters after ``ui://todos/main/``. It
#: changes with every UI build, so the golden holds ``<build>`` in its place.
_UI_BUILD = re.compile(r"(?<=ui://todos/main/)[0-9a-f]{12}(?![0-9a-f])")


def normalize_ui_build(text: str) -> str:
    """Replace each board UI build key in ``text`` with ``<build>``, as the golden holds it."""
    return _UI_BUILD.sub("<build>", text)


def local_env(db_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TODOS_")}
    return {**env, "TODOS_RUNTIME": "local", "TODOS_LOCAL_DB": str(db_path), **extra}


def stdio_client(db_path: Path) -> Client:
    return Client(
        StdioTransport(
            command=sys.executable, args=["-m", "mcp_todos"], env=local_env(db_path), cwd=str(REPO)
        )
    )


def token(db_path: Path) -> str:
    """The bearer token local HTTP requires for ``db_path``, read the way an installer does."""
    done = subprocess.run(
        [sys.executable, "-m", "mcp_todos_local", "--print-token"],
        env=local_env(db_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout.strip()


def http_client(port: int, db_path: Path) -> Client:
    """A client of ``todosforagents --http`` on ``port`` that presents the install's token."""
    return Client(f"http://127.0.0.1:{port}/mcp", auth=token(db_path))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def http_server(db_path: Path, **extra_env: str) -> Generator[int]:
    """``todosforagents --http`` on a free port; yields the port once it accepts."""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_todos_local", "--http"],
        env=local_env(db_path, TODOS_LOCAL_PORT=str(port), **extra_env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"the server exited: {proc.stdout.read() if proc.stdout else ''}"
                )
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("the server did not start listening") from None
                time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


async def surface(client: Client) -> dict[str, Any]:
    """The surface the golden test reads, from a connected client."""
    tools = sorted((_dump(t) for t in await client.list_tools()), key=lambda t: t["name"])
    resources = sorted((_dump(r) for r in await client.list_resources()), key=lambda r: r["uri"])
    templates = sorted(
        (_dump(t) for t in await client.list_resource_templates()),
        key=lambda t: t["uriTemplate"],
    )
    prompts = sorted((_dump(p) for p in await client.list_prompts()), key=lambda p: p["name"])
    skill = await client.read_resource("skill://todos/SKILL.md")
    skill_text = "".join(getattr(part, "text", "") for part in skill)
    return {
        "instructions": client.instructions,
        "capabilities": _dump(client.server_capabilities) if client.server_capabilities else {},
        "tools": tools,
        "prompts": prompts,
        "resources": resources,
        "resource_templates": templates,
        "skill_sha256": hashlib.sha256(skill_text.encode()).hexdigest(),
    }


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    """Call ``tool`` and return its structured result, failing on any error result."""
    result = await client.call_tool(tool, arguments, raise_on_error=False)
    data = result.structured_content
    assert not result.is_error, f"{tool} failed: {result.content}"
    assert isinstance(data, dict), f"{tool} returned no structured content"
    assert "error" not in data, f"{tool} returned {data}"
    assert data.get("next_step"), f"{tool} returned no next_step"
    return data


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0), f"{value} is not UTC"
    return parsed


async def exercise_every_tool(client: Client) -> set[str]:
    """Drive all 17 tools end to end on an empty database; returns the tools it called."""
    called: set[str] = set()

    async def run(tool: str, **arguments: Any) -> dict[str, Any]:
        called.add(tool)
        return await call(client, tool, **arguments)

    assert (await run("list_boards"))["boards"] == []
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    first = await run(
        "create_todo", title="Write the report", labels=["docs"], due_at=past, idempotency_key="k"
    )
    assert first["action"] == "created"
    goal = first["todo"]
    assert _utc(goal["due_at"]) < datetime.now(UTC) and _utc(goal["created_at"])
    again = await run("create_todo", title="Write the report", idempotency_key="k")
    assert again["action"] == "matched" and again["todo"]["id"] == goal["id"]

    plan = await run(
        "plan_todos",
        todos=[{"title": "Outline"}, {"title": "Draft"}, {"title": "Polish"}],
        parent_id=goal["id"],
        idempotency_key="plan",
    )
    assert plan["created"] == 3
    outline, draft, polish = (t["id"] for t in plan["todos"])
    replay = await run(
        "plan_todos",
        todos=[{"title": "Outline"}, {"title": "Draft"}, {"title": "Polish"}],
        parent_id=goal["id"],
        idempotency_key="plan",
    )
    assert replay["created"] == 0

    assert (await run("update_todo", todo_id=goal["id"], details="Q3 numbers"))["todo"][
        "details"
    ] == "Q3 numbers"
    assert (await run("start_todo", todo_id=goal["id"]))["todo"]["status"] == "in_progress"
    blocked = await run("block_todo", todo_id=goal["id"], reason="waiting on finance")
    assert blocked["todo"]["blocked_reason"] == "waiting on finance"
    assert (await run("reopen_todo", todo_id=goal["id"]))["todo"]["status"] == "open"
    done = await run("complete_todo", todo_id=outline)
    assert done["todo"]["status"] == "done" and _utc(done["todo"]["completed_at"])
    assert (await run("cancel_todo", todo_id=polish))["todo"]["status"] == "cancelled"

    board_id = goal["board_id"]
    open_lane = [t["id"] for t in (await run("list_todos", status="open"))["todos"]]
    reordered = list(reversed(open_lane))
    assert (await run("reorder_todos", board_id=board_id, status="open", todo_ids=reordered))[
        "ordered"
    ] == len(reordered)

    labelled = await run("list_todos", label="docs")
    assert [t["id"] for t in labelled["todos"]] == [goal["id"]]
    page = await run("list_todos", limit=2)
    assert page["next_cursor"]
    rest = await run("list_todos", limit=2, cursor=page["next_cursor"])
    assert {t["id"] for t in page["todos"]}.isdisjoint(t["id"] for t in rest["todos"])

    whole = await run("get_todo", todo_id=goal["id"])
    assert whole["todo"]["details"] == "Q3 numbers"
    assert whole["counts"] == {"steps": 3, "steps_done": 2}
    assert whole["recent_activity"]

    assert (await run("get_next_todo"))["todo"]["status"] == "open"
    summary = await run("get_board_summary")
    assert summary["totals"]["overdue"] == 1
    assert [r["id"] for r in (await run("list_todos", query="report"))["todos"]] == [goal["id"]]

    side = (await run("create_board", name="Side"))["board"]
    assert (await run("update_board", board_id=side["id"], name="Side quest"))["board"][
        "name"
    ] == "Side quest"
    assert len((await run("list_boards"))["boards"]) == 2

    archived = await run("archive_todo", todo_id=draft)
    assert archived["todo"]["archived_at"]
    assert draft not in {t["id"] for t in (await run("list_todos"))["todos"]}
    return called
