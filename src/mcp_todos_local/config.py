"""Where the local database lives and which port local HTTP uses."""

from __future__ import annotations

import os
import sys
from pathlib import Path

DB_ENV = "TODOS_LOCAL_DB"
PORT_ENV = "TODOS_LOCAL_PORT"
DEFAULT_PORT = 8765
#: Local HTTP binds here and nowhere else; there is no setting to change it.
LOOPBACK_HOST = "127.0.0.1"
APP_DIR = "todosforagents"


def data_dir() -> Path:
    """The per-user data directory this platform keeps application data in."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / APP_DIR
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_DIR


def database_path() -> Path:
    """``TODOS_LOCAL_DB`` when set and non-empty, else ``todos.db`` in :func:`data_dir`."""
    configured = (os.environ.get(DB_ENV) or "").strip()
    return Path(configured).expanduser() if configured else data_dir() / "todos.db"


def http_port() -> int:
    """``TODOS_LOCAL_PORT`` when set, else :data:`DEFAULT_PORT`."""
    raw = (os.environ.get(PORT_ENV) or "").strip()
    if not raw:
        return DEFAULT_PORT
    port = int(raw)
    if not 0 < port < 65536:
        raise ValueError(f"{PORT_ENV} must be a TCP port (1-65535), got {raw!r}")
    return port
