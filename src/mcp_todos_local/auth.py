"""The per-install token local HTTP requires.

Loopback binding and the ``Origin`` and ``Host`` checks (:mod:`.http`) keep browser pages and
other machines out, but not other processes on this machine. Local HTTP therefore also requires
``Authorization: Bearer <token>``, where the token is a secret only the installing user can
read:

- It lives beside the database file, at ``<database>.token`` (:func:`token_path`), so each
  database has its own and an installer finds it from the same setting the server uses.
- It is made on first use with :func:`secrets.token_urlsafe` and created owner-only (``0600``)
  on POSIX. On Windows the file inherits the per-user data directory's access control.
- Later starts reuse it, so a client configured once keeps working.

``todosforagents --print-token`` prints it for an installer. stdio needs no token: a process
that can start the server already has the database.

One route is public: ``GET`` on the Server Card (``<mcp-path>/server-card``). It holds only the
server's name, version, description, icon and placements, and a host reads it before it has
any credential, so it is served without the token (the loopback checks still apply).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from collections.abc import Collection
from pathlib import Path

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

TOKEN_SUFFIX = ".token"


class TokenError(RuntimeError):
    """The token file exists but holds no token."""


def token_path(db_path: Path) -> Path:
    """Where the token for the database at ``db_path`` is kept."""
    return db_path.with_name(db_path.name + TOKEN_SUFFIX)


def load_or_create(path: Path) -> str:
    """The token stored at ``path``, made and stored owner-only first if there is none.

    Processes starting together agree on one token: each writes a candidate to a private
    temporary file and links it into place, which fails if another got there first, and then
    every process reads whichever token was linked.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        candidate = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as out:
                out.write(secrets.token_urlsafe(32) + "\n")
            try:
                os.link(candidate, path)
            except FileExistsError:
                pass
        finally:
            candidate.unlink(missing_ok=True)
    token = path.read_text(encoding="ascii").strip()
    if not token:
        raise TokenError(f"{path} holds no token. Delete it, and a new one is made on next start.")
    return token


def _presented(scope: Scope) -> bytes | None:
    """The credential of a ``Bearer`` ``Authorization`` header, if the request carries one."""
    for name, value in scope["headers"]:
        if name.lower() == b"authorization":
            scheme, _, credential = value.partition(b" ")
            return credential.strip() if scheme.lower() == b"bearer" else None
    return None


class BearerGuard:
    """ASGI middleware that refuses, with 401, an HTTP or WebSocket request without the token.

    The comparison is constant-time, and the refusal says nothing about the token. A ``GET`` on
    one of ``public_paths`` (exact paths) needs no token.
    """

    def __init__(self, app: ASGIApp, token: str, public_paths: Collection[str] = ()) -> None:
        self.app = app
        self._token = token.encode("ascii")
        self._public_paths = frozenset(public_paths)

    def authorized(self, scope: Scope) -> bool:
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and scope.get("path") in self._public_paths
        ):
            return True
        presented = _presented(scope)
        return presented is not None and hmac.compare_digest(presented, self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not self.authorized(scope):
            refusal = PlainTextResponse(
                "Unauthorized.", status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)
