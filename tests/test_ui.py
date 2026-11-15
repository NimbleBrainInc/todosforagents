"""UI resource loader — the ``ui://todos/main`` shell.

These run WITHOUT a database on purpose: ``mcp_todos.ui`` imports only the
stdlib, so the loader suite both locks the file-read contract and proves the
security-relevant invariant that serving the UI never opens a database
session — the shell stays servable when the database is down. The registration test imports ``mcp_todos.server`` (its engine is lazy, so
no DB is touched at import time) and asserts the resource is registered as
``text/html;profile=mcp-app``, the MIME type the MCP Apps spec defines.
"""

import asyncio
import hashlib

import mcp_todos.ui as ui_mod


def test_load_ui_returns_placeholder_when_unbuilt(tmp_path, monkeypatch):
    # Point at a dir with no built bundle -> the "please build" placeholder.
    monkeypatch.setattr(ui_mod, "_UI_DIR", tmp_path / "dist")
    html = ui_mod.load_ui()
    assert "not built" in html.lower()
    assert "<html" in html.lower()


def test_load_ui_returns_built_html(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    built = "<!doctype html><html lang='en'><title>To-dos</title></html>"
    dist.joinpath("index.html").write_text(built, encoding="utf-8")
    monkeypatch.setattr(ui_mod, "_UI_DIR", dist)
    assert ui_mod.load_ui() == built


def test_ui_resource_registered_as_mcp_app_html():
    # The engine is lazy, so importing the server touches no database.
    from mcp_todos.server import mcp

    resource = asyncio.run(mcp.get_resource("ui://todos/main"))
    assert resource is not None, "ui://todos/main resource is not registered"
    assert str(resource.uri) == "ui://todos/main"
    assert resource.mime_type == "text/html;profile=mcp-app"


def test_ui_resource_serves_nonempty_html():
    # The resource reader returns the loader's HTML (bare file read, no DB).
    from mcp_todos.server import mcp

    result = asyncio.run(mcp.read_resource("ui://todos/main"))
    entry = result.contents[0]
    assert entry.mime_type == "text/html;profile=mcp-app"
    body = entry.content
    assert isinstance(body, str)
    assert len(body) > 0
    assert "<html" in body.lower() or "<!doctype" in body.lower()


def test_todos_ui_dir_overrides_the_packaged_ui(tmp_path, monkeypatch):
    # A deployment that places the built UI itself (a container image, say) is served from there.
    monkeypatch.setenv("TODOS_UI_DIR", str(tmp_path))
    assert ui_mod._ui_dir() == tmp_path
    monkeypatch.setenv("TODOS_UI_DIR", "")
    assert ui_mod._ui_dir() in (ui_mod._PACKAGED_UI_DIR, ui_mod._SOURCE_UI_DIR)


def _read(uri: str):
    from mcp_todos.server import mcp

    [entry] = asyncio.run(mcp.read_resource(uri)).contents
    return entry


def test_tools_bind_to_the_hash_of_the_served_html():
    # The key names exactly the bytes served, so a new UI build is a new URI.
    from mcp_todos import server

    served = _read(server.BOARD_UI_URI).content
    assert isinstance(served, str)
    key = hashlib.sha256(served.encode("utf-8")).hexdigest()[:12]
    assert server.BOARD_UI_BOUND_URI == f"ui://todos/main/{key}"
    for name in ("get_board_summary", "get_todo"):
        tool = asyncio.run(server.mcp.get_tool(name))
        assert tool is not None
        assert (tool.meta or {})["ui"]["resourceUri"] == server.BOARD_UI_BOUND_URI


def test_every_board_ui_uri_serves_the_same_html_and_meta():
    # The bound key, a key from an older build, and the listed bare URI all resolve.
    from mcp_todos import server

    uris = [server.BOARD_UI_BOUND_URI, "ui://todos/main/abcdefabcdef", server.BOARD_UI_URI]
    entries = [_read(uri) for uri in uris]
    for entry in entries:
        assert entry.mime_type == "text/html;profile=mcp-app"
        assert entry.content == server.BOARD_UI_HTML
        assert entry.meta == {"ui": {"csp": {}, "prefersBorder": True}}


def test_board_ui_is_read_once_at_import(monkeypatch):
    # Reads serve the copy taken at import, the one the key hashes, never a fresh file read.
    from mcp_todos import server

    assert server.BOARD_UI_HTML == ui_mod.load_ui()

    def _fail() -> str:
        raise AssertionError("load_ui called after import")

    monkeypatch.setattr(server, "load_ui", _fail)
    monkeypatch.setattr(ui_mod, "load_ui", _fail)
    for uri in (server.BOARD_UI_URI, server.BOARD_UI_BOUND_URI):
        assert _read(uri).content == server.BOARD_UI_HTML
