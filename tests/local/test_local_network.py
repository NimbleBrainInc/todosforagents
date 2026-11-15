"""Local HTTP is reachable from this machine only.

The server binds to loopback, with no setting that changes it, and refuses a request whose
``Origin`` or ``Host`` is not a loopback one (the MCP specification's rule for local servers,
and the DNS-rebinding defence), or that arrives from a non-loopback client address.
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from local_harness import http_server, token

from mcp_todos_local.config import LOOPBACK_HOST
from mcp_todos_local.http import refusal

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
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _scope(client: str = "127.0.0.1", **headers: str) -> dict[str, Any]:
    return {
        "type": "http",
        "client": (client, 50000),
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }


def _post(port: int, **headers: str) -> int:
    """POST an initialize to /mcp with ``headers`` (``Host`` included, when given)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        sent = {**HEADERS, **headers}
        conn.putrequest("POST", "/mcp", skip_host="Host" in sent)
        body = json.dumps(INIT).encode()
        for name, value in {**sent, "Content-Length": str(len(body))}.items():
            conn.putheader(name, value)
        conn.endheaders(body)
        return conn.getresponse().status
    finally:
        conn.close()


def _primary_address() -> str | None:
    """This machine's address on its default route, if it has one (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


@pytest.mark.parametrize(
    "scope",
    [
        _scope(host="127.0.0.1:8765"),
        _scope(host="localhost:8765", origin="http://localhost:3000"),
        _scope(host="[::1]:8765", origin="https://127.0.0.1"),
        _scope(client="::1", host="localhost"),
    ],
)
def test_loopback_requests_are_allowed(scope: dict[str, Any]) -> None:
    assert refusal(scope) is None


@pytest.mark.parametrize(
    "scope",
    [
        _scope(client="192.168.1.20", host="127.0.0.1:8765"),
        _scope(host="todos.example:8765"),
        _scope(host="127.0.0.1:8765", origin="https://evil.example"),
        _scope(host="127.0.0.1:8765", origin="null"),
        _scope(host="127.0.0.1:8765", origin="http://127.0.0.1.evil.example"),
        _scope(host="127.0.0.1:8765", origin="file://localhost"),
    ],
)
def test_other_requests_are_refused(scope: dict[str, Any]) -> None:
    assert refusal(scope) is not None


def test_the_bind_address_is_loopback() -> None:
    assert LOOPBACK_HOST == "127.0.0.1"


def test_the_server_answers_on_loopback_and_refuses_a_foreign_origin(tmp_path: Path) -> None:
    db = tmp_path / "todos.db"
    with http_server(db) as port:
        # The right token never admits a foreign Origin or Host: those checks run first.
        auth = f"Bearer {token(db)}"
        ok = _post(port, Authorization=auth)
        same_machine = _post(port, Authorization=auth, Origin=f"http://localhost:{port}")
        foreign = _post(port, Authorization=auth, Origin="https://evil.example")
        rebound = _post(port, Authorization=auth, Host="evil.example")
    assert ok == 200
    assert same_machine == 200
    assert foreign == 403
    assert rebound == 403


def test_the_server_is_not_reachable_off_loopback(tmp_path: Path) -> None:
    address = _primary_address()
    if address is None:
        pytest.skip("this machine has no non-loopback address to probe")
    # Settings a server might read for its bind address change nothing here.
    with http_server(tmp_path / "todos.db", HOST="0.0.0.0", TODOS_LOCAL_HOST="0.0.0.0") as port:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        with pytest.raises(OSError):
            socket.create_connection((address, port), timeout=2).close()
