"""Reading a tool's error result, and holding it to the one error shape.

Every failure is a tool result flagged ``isError`` whose body is
``{"error": {"code", "message", "retryable", ...}, "next_step"}`` and whose text is that body as
JSON. :func:`error_of` takes the result a tool function returns (a ``ToolResult``) or the one a
client receives (a ``CallToolResult``).
"""

from __future__ import annotations

import json
from typing import Any


def error_of(result: Any) -> dict[str, Any]:
    """The error ``result`` carries, flattened to its envelope plus ``next_step``.

    Fails unless ``result`` is flagged as an error and its body and text have the one shape.
    """
    assert not isinstance(result, dict), f"an ordinary result, not an error: {result}"
    assert result.is_error is True, f"not flagged isError: {result}"
    body = result.structured_content
    assert isinstance(body, dict) and set(body) == {"error", "next_step"}, body
    envelope = body["error"]
    assert isinstance(envelope["code"], str) and envelope["code"]
    assert isinstance(envelope["message"], str) and envelope["message"]
    assert isinstance(envelope["retryable"], bool)
    assert isinstance(body["next_step"], str) and body["next_step"].strip()
    assert json.loads(result.content[0].text) == body
    return {**envelope, "next_step": body["next_step"]}
