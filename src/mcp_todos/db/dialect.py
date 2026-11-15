"""What the tables and store use where Postgres and SQLite differ.

The store runs on Postgres (with row-level security) and on SQLite (one principal). Each
helper here picks the dialect's form of one construct, so the store's queries read the same
on both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, Table, exists, func, select, type_coerce
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.types import TypeDecorator

__all__ = ["JSONType", "RowId", "UTCDateTime", "insert_for", "json_array_contains"]

#: JSONB on Postgres, JSON text on SQLite.
JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")

#: An auto-incrementing key: SQLite generates one only for an ``INTEGER PRIMARY KEY``.
RowId = BigInteger().with_variant(Integer(), "sqlite")


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that is timezone-aware, in UTC, on every backend.

    ``timestamptz`` on Postgres. SQLite stores no zone, so the value is stored as UTC and the
    zone re-attached on read. A naive value bound to it is taken as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def insert_for(conn: AsyncConnection, table: Table) -> Any:
    """The dialect's ``INSERT``, which supports ``on_conflict_do_nothing`` with an index
    predicate on both Postgres and SQLite."""
    module = sqlite if conn.dialect.name == "sqlite" else postgresql
    return module.insert(table)


def json_array_contains(conn: AsyncConnection, column: Any, value: str) -> Any:
    """``column`` (a JSON array) holds ``value``: ``@>`` on Postgres, ``json_each`` on SQLite."""
    if conn.dialect.name == "sqlite":
        each = func.json_each(column).table_valued("value")
        return exists(select(1).select_from(each).where(each.c.value == value))
    return type_coerce(column, postgresql.JSONB()).contains([value])
