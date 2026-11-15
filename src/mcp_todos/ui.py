"""UI resource loader: the board UI, served as one static HTML file.

Serves the built UI (``ui/dist/index.html``) as a **static, identity-free** asset.
The HTML carries NO tenant/workspace data: every datum is fetched at runtime through
the host tool bridge, whose calls carry verified identity. So this loader is a
bare file read — it opens NO session and touches NO database, and stays servable even
when the database is unreachable (the shell loads and can show a graceful error).

Resolution order, the first that applies:
  * ``$TODOS_UI_DIR`` when set and non-empty: a deployment that places the built UI itself.
  * ``ui_dist/`` inside this package, when it holds ``index.html``: every installed wheel
    carries the built UI there (``hatch_build.py``), so an install serves it with no setting
    and no source checkout.
  * ``<repo root>/ui/dist`` relative to this file: an editable install running from the
    source tree, after ``cd ui && npm run build``.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Where a wheel carries the built UI.
_PACKAGED_UI_DIR = Path(__file__).resolve().parent / "ui_dist"
# ui.py is at <repo root>/src/mcp_todos/ui.py in a source tree, so parents[2] is the root.
_SOURCE_UI_DIR = Path(__file__).resolve().parents[2] / "ui" / "dist"


def _ui_dir() -> Path:
    configured = (os.environ.get("TODOS_UI_DIR") or "").strip()
    if configured:
        return Path(configured)
    if (_PACKAGED_UI_DIR / "index.html").is_file():
        return _PACKAGED_UI_DIR
    return _SOURCE_UI_DIR


_UI_DIR = _ui_dir()

_NOT_BUILT = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'></head>"
    '<body style="font-family:system-ui,sans-serif;padding:2rem;color:#374151">'
    "<p>To-dos UI is not built. Run <code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code>.</p>"
    "</body></html>"
)


def load_ui() -> str:
    """Return the built UI HTML, or a build-me placeholder when it is absent."""
    index = _UI_DIR / "index.html"
    if index.exists():
        return index.read_text(encoding="utf-8")
    return _NOT_BUILT
