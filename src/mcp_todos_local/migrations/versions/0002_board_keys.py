"""board keys and per-board to-do numbers

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13

Adds ``board.key``, ``board.next_number`` and
``todo.number``, backfilled by the same rule (:func:`mcp_todos.keys.assign_board_keys`; the
default board takes ``TODO``, to-dos are numbered per board in ``created_at`` order, ties
broken by id).

``key`` and ``number`` stay nullable in the schema: the store sets
them on every insert, and this backfill sets them on every older row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from mcp_todos.keys import assign_board_keys

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill() -> None:
    bind = op.get_bind()
    boards = bind.execute(
        sa.text("SELECT id, name, is_default FROM board ORDER BY created_at, id")
    ).all()
    keys = assign_board_keys([(b[0], b[1], bool(b[2])) for b in boards])
    for board_id, key in keys.items():
        bind.execute(
            sa.text('UPDATE board SET "key" = :key WHERE id = :id'), {"key": key, "id": board_id}
        )
    op.execute(
        """
        UPDATE todo SET number = numbered.n
        FROM (
            SELECT id, row_number() OVER (PARTITION BY board_id ORDER BY created_at, id) AS n
            FROM todo
        ) AS numbered
        WHERE todo.id = numbered.id
        """
    )
    op.execute(
        "UPDATE board SET next_number = "
        "COALESCE((SELECT max(number) FROM todo WHERE todo.board_id = board.id), 0) + 1"
    )


def upgrade() -> None:
    op.add_column("board", sa.Column("key", sa.Text(), nullable=True))
    op.add_column(
        "board", sa.Column("next_number", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column("todo", sa.Column("number", sa.Integer(), nullable=True))

    _backfill()

    op.create_index("uq_board_key", "board", ["tenant_id", "workspace_id", "key"], unique=True)
    op.create_index(
        "uq_todo_number",
        "todo",
        ["tenant_id", "workspace_id", "board_id", "number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_todo_number", table_name="todo")
    op.drop_index("uq_board_key", table_name="board")
    with op.batch_alter_table("todo") as batch:
        batch.drop_column("number")
    with op.batch_alter_table("board") as batch:
        batch.drop_column("next_number")
        batch.drop_column("key")
