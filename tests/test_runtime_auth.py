"""A runtime's auth provider gates the HTTP transport, and its verified token is the identity.

The runtime is chosen after ``server`` builds ``mcp``, so its provider cannot go to the
``FastMCP`` constructor. :func:`mcp_todos.server.install` sets ``mcp.auth`` from
``Runtime.auth()`` instead, before the HTTP app is built. These tests install a runtime whose
provider is a ``RemoteAuthProvider`` over a ``JWTVerifier`` and drive the HTTP app it yields:

- a request with no token, or one the verifier rejects, is refused with 401 and a
  ``WWW-Authenticate`` challenge naming the protected resource metadata, before any tool
  runs;
- a request with a valid token reaches the tools, and ``identity()`` reads the verified
  token's claims (through FastMCP's ``get_access_token``), so the rows it writes carry them.

The local runtime returns no provider: it guards HTTP itself (``tests/local``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from conftest import RUNTIME
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth import AuthProvider, RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.dependencies import get_access_token
from pydantic import AnyHttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.types import ASGIApp

from mcp_todos import server
from mcp_todos.db import board
from mcp_todos.runtime import AuthError, Limits, Scope, TodoEvent
from mcp_todos_local.runtime import LOCAL_SCOPE, LocalRuntime

ISSUER = "https://auth.example.com"
RESOURCE = "https://mcp.example.com/mcp"
BASE = "https://mcp.example.com"
KEYS = RSAKeyPair.generate()


class TokenRuntime:
    """A runtime with many principals: the verified bearer token names the caller.

    Its sessions open on the test's SQLite file through the suite's local runtime; that is
    enough here, because these tests check where identity comes from, not isolation.
    """

    name = "token-test"

    def __init__(self) -> None:
        self.identities: list[Scope] = []

    def auth(self) -> AuthProvider | None:
        return RemoteAuthProvider(
            token_verifier=JWTVerifier(
                public_key=KEYS.public_key, issuer=ISSUER, audience=RESOURCE
            ),
            authorization_servers=[AnyHttpUrl(ISSUER)],
            base_url=BASE,
        )

    def identity(self) -> Scope:
        token = get_access_token()
        if token is None:
            raise AuthError("no verified token")
        scope = Scope(
            tenant_id=token.claims["tenant"],
            workspace_id=token.claims["workspace"],
            user_id=token.claims["sub"],
        )
        self.identities.append(scope)
        return scope

    def session(self, scope: Scope) -> Any:
        return RUNTIME.session(LOCAL_SCOPE)

    def limits(self, scope: Scope) -> Limits | None:
        return None

    def record(self, event: TodoEvent, count: int = 1) -> None:
        return None

    def attach(self, mcp: FastMCP) -> None:
        return None

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None]:
        yield

    def wrap_http(self, app: ASGIApp) -> ASGIApp:
        return app


def _token(**claims: Any) -> str:
    return KEYS.create_token(
        subject="user-1",
        issuer=ISSUER,
        audience=RESOURCE,
        additional_claims={"tenant": "tenant-a", "workspace": "ws-1", **claims},
    )


@pytest.fixture
def token_runtime(monkeypatch: pytest.MonkeyPatch) -> TokenRuntime:
    """``TokenRuntime`` installed in place of the suite's local runtime, for one test."""
    monkeypatch.setattr(server, "_runtime", None)
    monkeypatch.setattr(server.mcp, "auth", None)
    rt = TokenRuntime()
    server.install(rt)
    return rt


@asynccontextmanager
async def serving(rt: TokenRuntime) -> AsyncGenerator[ASGIApp]:
    """The HTTP app the installed runtime serves, with its lifespan running."""
    inner = server.mcp.http_app()
    async with inner.router.lifespan_context(inner):
        yield rt.wrap_http(inner)


def _http(app: ASGIApp) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_install_sets_the_runtimes_auth_provider(token_runtime: TokenRuntime) -> None:
    assert isinstance(server.mcp.auth, RemoteAuthProvider)


def test_the_local_runtime_has_no_auth_provider() -> None:
    # It guards local HTTP with its own loopback checks and bearer token.
    assert LocalRuntime().auth() is None
    assert server.mcp.auth is None


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer not-a-jwt", f"Bearer {_token()}x"],
    ids=["no token", "garbage", "bad signature"],
)
async def test_a_request_without_a_verified_token_is_challenged(
    token_runtime: TokenRuntime, authorization: str | None
) -> None:
    headers = dict(_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    async with serving(token_runtime) as app, _http(app) as client:
        response = await client.post("/mcp", json=_INITIALIZE, headers=headers)
        metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer")
    assert (
        'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == RESOURCE
    assert metadata.json()["authorization_servers"] == [f"{ISSUER}/"]
    assert token_runtime.identities == []


async def test_a_token_for_another_audience_is_challenged(token_runtime: TokenRuntime) -> None:
    other = KEYS.create_token(
        subject="user-1",
        issuer=ISSUER,
        audience="https://elsewhere.example.com/mcp",
        additional_claims={"tenant": "tenant-a", "workspace": "ws-1"},
    )
    async with serving(token_runtime) as app, _http(app) as client:
        response = await client.post(
            "/mcp", json=_INITIALIZE, headers={**_HEADERS, "Authorization": f"Bearer {other}"}
        )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers


async def test_a_verified_token_is_the_callers_identity(token_runtime: TokenRuntime) -> None:
    async with serving(token_runtime) as app:

        def in_process(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            **kwargs: Any,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                headers=headers,
                timeout=timeout,
                auth=auth,
                **kwargs,
            )

        transport = StreamableHttpTransport(
            f"{BASE}/mcp", auth=_token(), httpx_client_factory=in_process
        )
        async with Client(transport) as client:
            result = await client.call_tool("create_board", {"name": "Launch plan"})
    assert result.is_error is False
    assert token_runtime.identities == [
        Scope(tenant_id="tenant-a", workspace_id="ws-1", user_id="user-1")
    ]

    async with RUNTIME.session(LOCAL_SCOPE) as conn:
        assert isinstance(conn, AsyncConnection)
        rows = (await conn.execute(select(board.c.tenant_id, board.c.workspace_id))).all()
    assert [tuple(r) for r in rows] == [("tenant-a", "ws-1")]
