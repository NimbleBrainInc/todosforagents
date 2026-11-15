"""Local HTTP requires the per-install token; stdio does not.

The token is made once, owner-only, beside the database, and reused on every later start.
A request without it, or with a wrong one, gets 401 and nothing that describes the token.
"""

from __future__ import annotations

import http.client
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from local_harness import http_client, http_server, local_env, stdio_client, token

from mcp_todos_local.auth import TokenError, load_or_create, token_path

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}


def _post(port: int, **headers: str) -> tuple[int, str, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "POST",
            "/mcp",
            body=json.dumps(INIT),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **headers,
            },
        )
        response = conn.getresponse()
        return response.status, response.read().decode(), dict(response.getheaders())
    finally:
        conn.close()


def test_the_token_is_made_once_and_reused(tmp_path: Path) -> None:
    path = token_path(tmp_path / "data" / "todos.db")
    assert path == tmp_path / "data" / "todos.db.token"
    first = load_or_create(path)
    assert len(first) >= 43  # 32 random bytes, URL-safe base64
    assert load_or_create(path) == first
    assert [p.name for p in path.parent.iterdir()] == ["todos.db.token"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_token_file_is_owner_only(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        path = token_path(tmp_path / "todos.db")
        load_or_create(path)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_empty_token_file_is_refused(tmp_path: Path) -> None:
    path = token_path(tmp_path / "todos.db")
    path.write_text("\n")
    with pytest.raises(TokenError):
        load_or_create(path)


def test_print_token_needs_no_runtime_and_matches_the_file(tmp_path: Path) -> None:
    db = tmp_path / "todos.db"
    env = {k: v for k, v in local_env(db).items() if k != "TODOS_RUNTIME"}
    done = subprocess.run(
        [sys.executable, "-m", "mcp_todos_local", "--print-token"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    printed = done.stdout.strip()
    assert printed == token_path(db).read_text().strip()
    assert token(db) == printed


def test_the_server_keeps_its_token_across_restarts(tmp_path: Path) -> None:
    db = tmp_path / "todos.db"
    with http_server(db):
        first = token_path(db).read_text()
    with http_server(db):
        second = token_path(db).read_text()
    assert first == second


def test_http_without_the_right_token_is_401(tmp_path: Path) -> None:
    db = tmp_path / "todos.db"
    with http_server(db) as port:
        secret = token(db)
        missing = _post(port)
        wrong = _post(port, Authorization="Bearer not-the-token")
        prefix = _post(port, Authorization=f"Bearer {secret[:-1]}")
        other_scheme = _post(port, Authorization=f"Basic {secret}")
        bare = _post(port, Authorization=secret)
        right = _post(port, Authorization=f"Bearer {secret}")
        lower = _post(port, Authorization=f"bearer {secret}")
    for status, body, headers in (missing, wrong, prefix, other_scheme, bare):
        assert status == 401
        assert secret not in body and "token" not in body.lower()
        assert headers.get("www-authenticate", headers.get("WWW-Authenticate")) == "Bearer"
    assert right[0] == 200
    assert lower[0] == 200


async def test_a_client_without_the_token_cannot_call_tools(tmp_path: Path) -> None:
    db = tmp_path / "todos.db"
    with http_server(db) as port:
        with pytest.raises(Exception):  # noqa: B017 - the transport's 401 error type varies
            async with Client(f"http://127.0.0.1:{port}/mcp", auth="wrong") as client:
                await client.list_tools()
        async with http_client(port, db) as client:
            created = await client.call_tool("create_todo", {"title": "Authorized"})
    assert created.structured_content is not None
    assert created.structured_content["todo"]["title"] == "Authorized"


async def test_stdio_needs_no_token_and_makes_none(tmp_path: Path) -> None:
    db = tmp_path / "todos.db"
    async with stdio_client(db) as client:
        result = await client.call_tool("list_boards", {})
    assert result.structured_content is not None
    assert not token_path(db).exists()
