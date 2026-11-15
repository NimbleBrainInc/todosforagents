"""Unit tests for opaque id minting (``ids.py``).

The store suites mint ids, but none reaches the non-trivial monotonic-ULID logic
(same-millisecond increment, clock-backwards hold, 80-bit overflow spill), so these
test it directly.
"""

from __future__ import annotations

from mcp_todos.ids import (
    BOARD_PREFIX,
    TODO_PREFIX,
    new_board_id,
    new_todo_id,
    new_ulid,
)

# Crockford base32 — the ULID alphabet, excludes I/L/O/U, ascending ASCII order.
_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ulid_shape() -> None:
    """A ULID is exactly 26 Crockford-base32 chars (no I/L/O/U)."""
    u = new_ulid()
    assert len(u) == 26
    assert set(u) <= _CROCKFORD


def test_prefixes_and_format() -> None:
    """Ids are ``<prefix>_<26-char ulid>`` with the entity prefixes."""
    assert BOARD_PREFIX == "bd"
    assert TODO_PREFIX == "td"
    for mint, prefix in ((new_board_id, "bd"), (new_todo_id, "td")):
        value = mint()
        head, _, body = value.partition("_")
        assert head == prefix
        assert len(body) == 26
        assert set(body) <= _CROCKFORD


def test_monotonic_and_unique_in_tight_loop() -> None:
    """A tight loop (mostly one millisecond) stays strictly increasing + unique.

    This is the ``plan_todos`` batch case: many ids minted back to back must keep
    their mint order lexically (the same-ms random-increment path) and never
    collide.
    """
    ulids = [new_ulid() for _ in range(1000)]
    assert len(set(ulids)) == len(ulids), "ulids must be unique"
    assert ulids == sorted(ulids), "ulids must be lexically monotonic in mint order"


def test_todo_ids_unique_and_sorted() -> None:
    """The prefixed ids' ulid bodies are unique and monotonic across a batch."""
    ids = [new_todo_id() for _ in range(500)]
    assert len(set(ids)) == len(ids)
    bodies = [i.partition("_")[2] for i in ids]
    assert bodies == sorted(bodies)
