"""SQLAlchemy Core table definitions for the To-dos working list.

Four design notes that are load-bearing:

  * **Open strings, never enums.** ``status`` / ``priority`` / ``assignee`` /
    ``source`` are plain ``Text`` with NO ``CHECK`` constraint or DB enum. The
    canonical ``status`` set (open / in_progress / blocked / done / cancelled) is
    enforced **app-side with graceful fallthrough**, not a DB ``CHECK`` — a
    ``CHECK`` would force a migration to add a state and is a bespoke-creep
    finding; reject it.
  * **Real edges, no name-denormalization.** Postgres FKs are authoritative from
    creation: ``todo.board_id`` (required → board), ``todo.parent_id`` (nullable,
    self-FK → todo). We denormalize immutable ids, never mutable display fields.
  * **Isolation is not defined here.** These definitions are the shape. On Postgres a
    runtime's schema history enables and FORCEs row-level security and installs a tenant+workspace
    policy keyed on per-transaction settings. Every row is tagged ``(tenant_id,
    workspace_id)`` — RLS is per-row and does NOT follow FKs, so the ``todo`` child and the
    append-only ``todo_activity`` each carry the pair too. The app connects as a role with
    no BYPASSRLS, so the policy is the actual boundary. A single-principal SQLite store
    stamps the same columns and needs no policy.
  * **One shape on both dialects.** Timestamps are :class:`UTCDateTime`, JSON is JSONB on
    Postgres, and each partial index carries its predicate for both dialects
    (:mod:`mcp_todos.db.dialect`).

Ids are opaque, prefixed ulids (``bd_``/``td_``) minted in the store layer —
never DB sequences or server defaults. ``todo_activity`` is the exception: it
uses a bigint autoincrement id, not a ulid.

Reserved-word note: ``order`` is a SQL keyword, so the to-do's sort position is
stored in a column named ``sort_order`` and mapped to the wire field ``order`` in
:meth:`mcp_todos.models.Todo.from_row`.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    text,
)

from mcp_todos.db.dialect import JSONType, RowId, UTCDateTime

metadata = MetaData()

board = Table(
    "board",
    metadata,
    Column("id", Text, primary_key=True),  # bd_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("name", Text, nullable=False),
    # The board's key, 2-5 of A-Z/0-9, unique in the workspace and fixed at creation
    # (mcp_todos.keys). A to-do's displayed key is ``<board key>-<number>``. The store sets
    # it on every insert and the schema histories backfilled every older row; the column
    # admits NULL only so a row written beside the store (a test that seeds raw rows)
    # still inserts. A NULL never collides in the unique index.
    Column("key", Text, nullable=True),
    # The number the board's next to-do takes. A create claims it in its own transaction,
    # so numbers are sequential per board and never reused.
    Column("next_number", Integer, nullable=False, server_default="1"),
    Column("description", Text, nullable=True),
    # Ordered presentation overlay: [{key, label, status, color?, wip_limit?}].
    # JSON, not relational — it is a per-board view, not queried row-by-row.
    Column("columns", JSONType, nullable=True),
    # Exactly one default board per workspace (partial-unique index below).
    Column("is_default", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
)

todo = Table(
    "todo",
    metadata,
    Column("id", Text, primary_key=True),  # td_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    # FK → board, required. Every to-do lives on a board (the workspace default if
    # none given). The edge is authoritative from creation.
    Column("board_id", Text, ForeignKey("board.id"), nullable=False),
    # Self-FK for step decomposition. Nullable; one level is the common case.
    Column("parent_id", Text, ForeignKey("todo.id"), nullable=True),
    # Sequential per board from 1, never reused: with the board's key it is the to-do's
    # displayed key (``MAG-3``). Set by the store on every insert and backfilled for older
    # rows; nullable for the same reason as ``board.key``.
    Column("number", Integer, nullable=True),
    Column("title", Text, nullable=False),
    Column("details", Text, nullable=True),  # markdown longer detail
    # Canonical status — the agent contract. Open string, NOT an enum/CHECK;
    # validated app-side with graceful fallthrough. v1 default `open`.
    Column("status", Text, nullable=False, server_default="open"),
    # Open string priority, NOT an enum. v1 default `normal`.
    Column("priority", Text, nullable=False, server_default="normal"),
    Column("labels", JSONType, nullable=True),  # open tags (string[])
    Column("assignee", Text, nullable=True),  # open string (agent / handle / null)
    # Sort position within the lane. ``order`` is a SQL keyword, so the column is
    # named ``sort_order`` and mapped to the wire field ``order`` in models.Todo.
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("due_at", UTCDateTime(), nullable=True),
    Column("blocked_reason", Text, nullable=True),  # set by block_todo, cleared on start/reopen
    Column("completed_at", UTCDateTime(), nullable=True),  # set by complete/cancel
    Column("archived_at", UTCDateTime(), nullable=True),  # soft-hide from default views
    # Optional retry-dedupe token (internal — never on the wire model): a retried
    # plan_todos step passing the same key collapses to the existing row under the
    # partial unique index below. NULL = a plain create.
    Column("idempotency_key", Text, nullable=True),
    Column("source", Text, nullable=True),  # provenance: agent / user / import
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
)

# Append-only audit trail: one row per mutation. RLS-policied like the
# rest. No updated_at — rows are written once, never mutated. A bigint
# autoincrement id, NOT a ulid.
todo_activity = Table(
    "todo_activity",
    metadata,
    Column("id", RowId, primary_key=True, autoincrement=True),
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("user_id", Text, nullable=True),
    Column("todo_id", Text, nullable=False),
    Column("action", String(32), nullable=False),
    Column("outcome", String(16), nullable=False),  # ok | denied | error
    Column("correlation_id", Text, nullable=True),
    # Optional structured context for the change record. Holds ONLY ids, counts,
    # and field NAMES — never PII/values — so the durable log stays attributable
    # without becoming a second copy of the data.
    Column("detail", JSONType, nullable=True),
    Column("at", UTCDateTime(), nullable=False, server_default=func.now()),
)

# --- Idempotency + invariant indexes ----------------------------
#
# Partial unique indexes scoped to (tenant_id, workspace_id). Partial
# (WHERE ... ) so only the populated/qualifying rows participate.

# Idempotency dedupe for plan_todos/create_todo: a retried call passing the same
# key collapses to one row under this partial unique index. Scoped PER BOARD
# (tenant/workspace lead, then board_id) so the same key reused on a DIFFERENT
# board yields distinct rows. Partial so NULL keys (the plain-create default)
# never collide.
uq_todo_idem = Index(
    "uq_todo_idem",
    todo.c.tenant_id,
    todo.c.workspace_id,
    todo.c.board_id,
    todo.c.idempotency_key,
    unique=True,
    postgresql_where=todo.c.idempotency_key.isnot(None),
    sqlite_where=todo.c.idempotency_key.isnot(None),
)

# Exactly one default board per workspace: a partial unique index on
# (tenant_id, workspace_id) WHERE is_default. Two default boards in one workspace
# would make "the default board" ambiguous.
uq_board_default = Index(
    "uq_board_default",
    board.c.tenant_id,
    board.c.workspace_id,
    unique=True,
    postgresql_where=board.c.is_default,
    sqlite_where=board.c.is_default,
)

# A board key names one board in its workspace.
uq_board_key = Index(
    "uq_board_key",
    board.c.tenant_id,
    board.c.workspace_id,
    board.c.key,
    unique=True,
)

# A number names one to-do on its board; this index also serves the key lookup.
uq_todo_number = Index(
    "uq_todo_number",
    todo.c.tenant_id,
    todo.c.workspace_id,
    todo.c.board_id,
    todo.c.number,
    unique=True,
)

# --- Query-path indexes (non-unique) ---------------------------------------
#
# Each leads with (tenant_id, workspace_id), the predicate row-level security puts on every
# read, then serves one read path: the lane view and get_next_todo, the step filter, the keyset
# listing by (created_at, id), the board roster, and the activity timeline. Every schema
# history creates these, and the parity test holds each history to this metadata.
Index(
    "ix_todo_lane",
    todo.c.tenant_id,
    todo.c.workspace_id,
    todo.c.board_id,
    todo.c.status,
    todo.c.sort_order,
    todo.c.id,
)
Index("ix_todo_parent_id", todo.c.tenant_id, todo.c.workspace_id, todo.c.parent_id)
Index("ix_todo_created_at_id", todo.c.tenant_id, todo.c.workspace_id, todo.c.created_at, todo.c.id)
Index(
    "ix_board_created_at_id",
    board.c.tenant_id,
    board.c.workspace_id,
    board.c.created_at,
    board.c.id,
)
Index(
    "ix_todo_activity_todo_id_at",
    todo_activity.c.tenant_id,
    todo_activity.c.workspace_id,
    todo_activity.c.todo_id,
    todo_activity.c.at,
)
