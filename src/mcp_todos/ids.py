"""To-dos' record ids: ``bd_<ulid>`` / ``td_<ulid>``.

The ids are the public contract other systems join on, so nothing reads the prefix: a record
keeps the id it was given. Every id is minted by the SDK's minter
(:func:`nimblebrain_server_sdk.new_prefixed_id`): opaque, never recycled, and sortable by mint
time, so a ``plan_todos`` batch keeps its order.

``todo_activity`` rows use a bigint autoincrement id and are not minted here.
"""

from __future__ import annotations

from nimblebrain_server_sdk import new_prefixed_id, new_ulid

__all__ = [
    "BOARD_PREFIX",
    "TODO_PREFIX",
    "new_board_id",
    "new_todo_id",
    "new_ulid",
]

# Entity prefixes. Opaque body follows the underscore.
BOARD_PREFIX = "bd"
TODO_PREFIX = "td"


def new_board_id() -> str:
    """``bd_<ulid>`` — opaque Board id."""
    return new_prefixed_id(BOARD_PREFIX)


def new_todo_id() -> str:
    """``td_<ulid>`` — opaque To-do id."""
    return new_prefixed_id(TODO_PREFIX)
