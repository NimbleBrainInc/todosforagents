"""initial schema: board + todo + todo_activity

Revision ID: 0001
Revises:
Create Date: 2026-11-13

The local history: the tables, columns and indexes of ``mcp_todos.db.tables``, for SQLite and
one principal, so there is no row-level security. Timestamps are stored as UTC
(``UTCDateTime`` re-attaches the zone on read), JSON as text, and ``todo_activity.id`` is an
``INTEGER PRIMARY KEY``, the only key SQLite generates values for.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "board",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("columns", sa.JSON(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "todo",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("board_id", sa.Text(), sa.ForeignKey("board.id"), nullable=False),
        sa.Column("parent_id", sa.Text(), sa.ForeignKey("todo.id"), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("priority", sa.Text(), server_default="normal", nullable=False),
        sa.Column("labels", sa.JSON(), nullable=True),
        sa.Column("assignee", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "todo_activity",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("todo_id", sa.Text(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Partial unique indexes: idempotent create, and one default board per workspace.
    op.create_index(
        "uq_todo_idem",
        "todo",
        ["tenant_id", "workspace_id", "board_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_board_default",
        "board",
        ["tenant_id", "workspace_id"],
        unique=True,
        sqlite_where=sa.text("is_default"),
    )

    # Query-path indexes.
    op.create_index(
        "ix_todo_lane",
        "todo",
        ["tenant_id", "workspace_id", "board_id", "status", "sort_order", "id"],
    )
    op.create_index("ix_todo_parent_id", "todo", ["tenant_id", "workspace_id", "parent_id"])
    op.create_index(
        "ix_todo_created_at_id", "todo", ["tenant_id", "workspace_id", "created_at", "id"]
    )
    op.create_index(
        "ix_board_created_at_id", "board", ["tenant_id", "workspace_id", "created_at", "id"]
    )
    op.create_index(
        "ix_todo_activity_todo_id_at",
        "todo_activity",
        ["tenant_id", "workspace_id", "todo_id", "at"],
    )


def downgrade() -> None:
    op.drop_table("todo_activity")
    op.drop_table("todo")
    op.drop_table("board")
