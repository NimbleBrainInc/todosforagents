"""``python -m mcp_todos``: serve stdio on the runtime ``TODOS_RUNTIME`` names.

Logs go to stderr only; stdout carries nothing but JSON-RPC. Exits non-zero, before serving,
when ``TODOS_RUNTIME`` is unset or names no installed runtime.
"""

from __future__ import annotations

import logging
import sys


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    from mcp_todos import server
    from mcp_todos.runtime import RuntimeSelectionError

    try:
        server.installed()
    except RuntimeSelectionError as exc:
        print(f"mcp-todos: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    server.mcp.run()


if __name__ == "__main__":
    main()
