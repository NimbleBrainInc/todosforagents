"""Local HTTP: reachable from this machine only.

The server binds to loopback (:data:`~mcp_todos_local.config.LOOPBACK_HOST`) with no setting
to change it, and :class:`LoopbackGuard` refuses, with 403, a request that

- comes from a client address that is not loopback (in case the app is served some other way);
- names a ``Host`` that is not a loopback name, which is how a DNS-rebinding page reaches a
  local server from a browser;
- carries an ``Origin`` that is not a loopback origin, as the MCP specification requires of a
  local server. A request with no ``Origin`` (a non-browser client) is allowed.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_NAMES = frozenset({"localhost"})


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname(authority: str) -> str | None:
    try:
        return urlsplit(f"//{authority}").hostname
    except ValueError:
        return None


def refusal(scope: Scope) -> str | None:
    """Why this request is refused, or ``None`` when it may proceed."""
    client = scope.get("client")
    if client is not None and not is_loopback(client[0]):
        return "Refused: this server accepts connections from this machine only."
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
    host = headers.get("host")
    if host is not None and not is_loopback(_hostname(host)):
        return "Refused: the Host header does not name this machine."
    origin = headers.get("origin")
    if origin is not None:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not is_loopback(parts.hostname):
            return "Refused: requests from this Origin are not allowed."
    return None


class LoopbackGuard:
    """ASGI middleware that applies :func:`refusal` to every HTTP and WebSocket request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            reason = refusal(scope)
            if reason is not None:
                await PlainTextResponse(reason, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
