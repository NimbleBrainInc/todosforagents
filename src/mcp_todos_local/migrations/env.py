"""Alembic environment for the local SQLite schema.

Runs only on the connection :func:`mcp_todos_local.schema.upgrade` passes in, inside its
``BEGIN IMMEDIATE`` transaction, so the upgrade commits or rolls back as one.
"""

from __future__ import annotations

from alembic import context

from mcp_todos.db.tables import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
