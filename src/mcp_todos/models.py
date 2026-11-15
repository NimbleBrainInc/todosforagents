"""Pydantic wire contracts for To-dos — Board, To-do, TodoActivity.

These models are the **public contract**: the exact shapes the MCP tools return
to the agent. They are deliberately decoupled from the database schema. The
precise rules:

- **Opaque, public string ids only** (``bd_<ulid>`` / ``td_<ulid>``). Never a DB
  sequence id, never a raw row id. (``todo_activity`` has a bigint id and is not
  a public, opaque-id entity — its wire shape carries no ``id``.)
- **Identity is implicit and never returned.** ``tenant_id`` and ``workspace_id``
  exist on every row in the database but MUST NOT appear in any wire model — the
  agent never sees who/where it is scoped to. ``ConfigDict(extra="forbid")``
  makes a stray DB column (e.g. ``tenant_id``) a hard error
  rather than a silent leak.
- **No SQLAlchemy types, no internal columns.** No ORM rows cross this boundary.
- **Open strings, not enums.** ``status`` / ``priority`` / ``assignee`` /
  ``source`` are plain ``str`` / ``str | None``; the canonical ``status`` set is
  validated app-side with graceful fallthrough, never a DB ``CHECK``.
- **Datetimes are ISO-8601.** Pydantic v2 serializes ``datetime`` to ISO-8601;
  store UTC-aware values.

Serialization note (``exclude_none``):
    Pydantic v2's ``ConfigDict`` has **no** ``exclude_none`` key — it is a
    ``model_dump`` argument, not model config. To omit ``None`` fields on the
    wire, serialize via ``model_dump(exclude_none=True)`` /
    ``model_dump_json(exclude_none=True)``, or use the :func:`dump` /
    :func:`dump_json` helpers below, which bake that in. ``model_config`` only
    sets ``extra="forbid"`` so a stray DB column is rejected rather than leaking.

Each model carries a ``from_row`` classmethod that maps a SQLAlchemy row/mapping
(or any attribute- or key-bearing object) to the wire model. The store layer
uses these as the single seam from DB row → public shape; only the listed wire
fields are read, so internal columns never escape.

Reserved-word note: the DB stores the sort position as ``sort_order`` (``order``
is a SQL keyword); the wire contract exposes it as ``order``. The
mapping happens in :meth:`Todo.from_row`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from mcp_todos.keys import format_todo_key


def _get(row: Any, key: str) -> Any:
    """Read ``key`` from a SQLAlchemy row, mapping, or plain attribute object.

    Supports ``Mapping`` access (``row["key"]``), SQLAlchemy ``Row._mapping`` /
    ORM attribute access, and falls back to ``None`` for absent optional fields.
    """
    # Mapping-like (dict, RowMapping, SQLAlchemy Row via ._mapping).
    mapping = getattr(row, "_mapping", row)
    try:
        if key in mapping:  # type: ignore[operator]
            return mapping[key]  # type: ignore[index]
    except TypeError:
        pass
    # Attribute-like (ORM instance, namedtuple-ish).
    return getattr(row, key, None)


#: The canonical statuses, the agent's contract. Tool inputs that take a status
#: declare this set, so a client sees it as an enum and an unknown status fails validation.
Status = Literal["open", "in_progress", "blocked", "done", "cancelled"]
STATUSES: tuple[str, ...] = get_args(Status)


# One step in a ``plan_todos`` call: the per-item input contract. The other models
# here are output wire contracts. It is typed so FastMCP coerces each field at the tool
# boundary (``due_at`` arrives as a JSON string and becomes a ``datetime``) and a bad item
# fails as a clean validation error (``invalid_request``) instead of a dropped field or an
# opaque 500. ``extra="forbid"`` rejects unknown keys. Byte-length caps and the
# status/blocked_reason rule stay in the store, which validates this model again so a direct
# store call (a dict) is coerced the same way. The class has no docstring because its
# docstring would become the item schema's description.
class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="The step's title.")
    details: str | None = Field(default=None, description="Optional markdown detail.")
    priority: str | None = Field(
        default=None,
        description="urgent, high, normal (the default) or low. Any other value is kept and "
        "ranks after low.",
    )
    labels: list[str] | None = Field(default=None, description="Open tags.")
    due_at: datetime | None = Field(default=None, description="Optional due time (ISO-8601).")
    status: Status | None = Field(
        default=None,
        description="The step's starting status; default open. done and cancelled set "
        "completed_at; blocked needs blocked_reason.",
    )
    blocked_reason: str | None = Field(
        default=None,
        description="Why the step is blocked. Required when status is blocked, and taken only "
        "then.",
    )


class Board(BaseModel):
    """A Board wire contract (``bd_<ulid>``)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id — the ecosystem join key.")
    key: str = Field(
        description="The board's key (2-5 of A-Z/0-9), fixed at creation; its to-dos are "
        "<key>-1, <key>-2, ..."
    )
    name: str
    description: str | None = None
    columns: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Ordered presentation overlay: [{key, label, status, color?, wip_limit?}]. "
            "Each column maps to a canonical status (v1: 1:1). Presentation only — the "
            "agent never reads column keys."
        ),
    )
    is_default: bool = Field(
        default=False,
        description="The workspace's default board (auto-created on first create_todo).",
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> Board:
        """Map a SQLAlchemy row/mapping to the wire model (store seam)."""
        return cls(
            id=_get(row, "id"),
            key=_get(row, "key"),
            name=_get(row, "name"),
            description=_get(row, "description"),
            columns=_get(row, "columns"),
            is_default=bool(_get(row, "is_default")),
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
        )


class Todo(BaseModel):
    """A To-do wire contract (``td_<ulid>``).

    ``order`` is the wire name for the DB ``sort_order`` column (``order`` is a SQL
    keyword; see :meth:`from_row`). ``key`` is the displayed key, ``<board key>-<number>``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")
    key: str = Field(
        description="Displayed key, <board key>-<number> (e.g. MAG-3); any tool argument "
        "that takes a to-do id also takes it."
    )
    board_id: str = Field(description="FK → Board; opaque id.")
    parent_id: str | None = Field(
        default=None, description="FK → To-do (self) for step decomposition; opaque id."
    )
    title: str
    details: str | None = Field(default=None, description="Markdown longer detail.")
    status: str = Field(
        description=(
            "Canonical status the agent reads/writes: open / in_progress / blocked / "
            "done / cancelled. Open validated string with graceful fallthrough; "
            "written only via the transition verbs."
        )
    )
    priority: str = Field(description="Open string: urgent / high / normal / low (default normal).")
    labels: list[str] | None = Field(default=None, description="Open tags.")
    assignee: str | None = Field(
        default=None, description="Open string (agent, a human handle, or null)."
    )
    order: int = Field(description="Sort position within its lane (DB column sort_order).")
    due_at: datetime | None = None
    blocked_reason: str | None = Field(
        default=None, description="Set by block_todo; cleared by start_todo/reopen_todo."
    )
    completed_at: datetime | None = Field(
        default=None, description="Set by complete_todo/cancel_todo; cleared by reopen_todo."
    )
    archived_at: datetime | None = Field(
        default=None, description="Soft-hide from default views without losing the row."
    )
    source: str | None = Field(default=None, description="Provenance: agent / user / import.")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any, board_key: str | None = None) -> Todo:
        """Map a SQLAlchemy row/mapping to the wire model (store seam).

        ``idempotency_key`` is internal (a retry-dedupe token) and is deliberately
        NOT a wire field. The DB ``sort_order`` column is exposed on the wire as
        ``order``. The key joins the board's key (``board_key``, else the row's
        ``board_key`` column) to the row's ``number``.
        """
        key = board_key if board_key is not None else _get(row, "board_key")
        return cls(
            id=_get(row, "id"),
            key=format_todo_key(key, _get(row, "number")),
            board_id=_get(row, "board_id"),
            parent_id=_get(row, "parent_id"),
            title=_get(row, "title"),
            details=_get(row, "details"),
            status=_get(row, "status"),
            priority=_get(row, "priority"),
            labels=_get(row, "labels"),
            assignee=_get(row, "assignee"),
            order=_get(row, "sort_order"),
            due_at=_get(row, "due_at"),
            blocked_reason=_get(row, "blocked_reason"),
            completed_at=_get(row, "completed_at"),
            archived_at=_get(row, "archived_at"),
            source=_get(row, "source"),
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
        )


class TodoActivity(BaseModel):
    """A TodoActivity wire contract — the append-only audit trail.

    The bigint DB ``id`` is internal and not a
    public opaque id, so it is not on the wire; ``detail`` carries only ids,
    counts, and field NAMES — never PII.
    """

    model_config = ConfigDict(extra="forbid")

    todo_id: str = Field(description="FK → To-do; opaque id.")
    action: str = Field(
        description="create / plan / update / start / complete / block / reopen / "
        "cancel / reorder / archive."
    )
    outcome: str = Field(description="ok / denied / error.")
    detail: dict[str, Any] | None = Field(
        default=None, description="Structured context: ids / counts / field names only."
    )
    at: datetime

    @classmethod
    def from_row(cls, row: Any) -> TodoActivity:
        """Map a SQLAlchemy row/mapping to the wire model (store seam)."""
        return cls(
            todo_id=_get(row, "todo_id"),
            action=_get(row, "action"),
            outcome=_get(row, "outcome"),
            detail=_get(row, "detail"),
            at=_get(row, "at"),
        )


def dump(model: BaseModel) -> dict[str, Any]:
    """Serialize a model to a dict with ``None`` fields omitted.

    Convenience wrapper for ``model.model_dump(exclude_none=True)`` — the
    documented wire shape.
    """
    return model.model_dump(exclude_none=True)


def dump_json(model: BaseModel) -> str:
    """Serialize a model to JSON with ``None`` fields omitted.

    Convenience wrapper for ``model.model_dump_json(exclude_none=True)``.
    """
    return model.model_dump_json(exclude_none=True)
