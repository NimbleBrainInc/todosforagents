"""Human-readable keys: a board's ``key`` and a to-do's ``<board key>-<number>``.

A board key is 2 to 5 characters, uppercase ``A``-``Z`` and ``0``-``9``, starting with a
letter, unique within a workspace and fixed when the board is created. Each to-do carries a
``number``, sequential on its board from 1 and never reused, so ``MAG-3`` names one to-do in a
workspace for as long as it exists. The ``td_<ulid>`` id stays the canonical id; a key is a
second way to name the same row.

Everything here is pure: the store and both schema histories call it, so a key derived at
creation and one derived by a migration's backfill follow one rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

__all__ = [
    "DEFAULT_BOARD_KEY",
    "assign_board_keys",
    "derive_board_key",
    "format_todo_key",
    "normalize_board_key",
    "parse_board_key",
    "parse_todo_key",
]

#: The workspace default board's key. Reserved: no other board takes it.
DEFAULT_BOARD_KEY = "TODO"

_BOARD_KEY = re.compile(r"[A-Z][A-Z0-9]{1,4}")
# A to-do key as typed: any case, the board key, a hyphen, then the number. The number is
# capped at nine digits so a parsed key always fits the integer column.
_TODO_KEY = re.compile(r"([A-Za-z][A-Za-z0-9]{1,4})-([0-9]{1,9})")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

#: Words a derived key leaves out, because nearly every board name could carry them.
_GENERIC_WORDS = frozenset({"board", "list", "the", "and", "of", "for", "my", "our"})
_MAX_LEN = 5
_FALLBACK = "TB"


def normalize_board_key(value: str) -> str:
    """An explicit board key, uppercased; ``ValueError`` if it breaks the rule."""
    key = value.strip().upper()
    if not _BOARD_KEY.fullmatch(key):
        raise ValueError(
            "key must be 2-5 characters, letters A-Z and digits 0-9, starting with a letter "
            f"(got {value!r})"
        )
    return key


def _base_key(name: str) -> str:
    words = [_NON_ALNUM.sub("", w) for w in name.split()]
    words = [w for w in words if w and w.lower() not in _GENERIC_WORDS]
    if len(words) == 1:
        base = words[0][:3]
    else:
        base = "".join(w[0] for w in words[:_MAX_LEN])
    base = base.upper()
    if len(base) < 2:
        return _FALLBACK
    if base[0].isdigit():
        return ("B" + base)[:_MAX_LEN]
    return base


def derive_board_key(name: str, taken: Iterable[str]) -> str | None:
    """The key a board named ``name`` gets, given the keys ``taken`` in its workspace.

    Generic words drop out, each word keeps its letters and digits, then one word gives its
    first three characters and several give their initials (up to five). A base shorter than
    two characters becomes ``TB``, and one starting with a digit gets a ``B`` in front. On a
    clash the lowest free digit 2-9 is appended, the base trimmed so the whole stays within
    five. ``None`` when every suffix is taken: the caller must ask for an explicit key.
    The default board's key is always counted as taken.
    """
    used = {k.upper() for k in taken} | {DEFAULT_BOARD_KEY}
    base = _base_key(name)
    if base not in used:
        return base
    for digit in range(2, 10):
        candidate = base[: _MAX_LEN - 1] + str(digit)
        if candidate not in used:
            return candidate
    return None


def assign_board_keys(boards: Sequence[tuple[str, str, bool]]) -> dict[str, str]:
    """Keys for one workspace's existing boards, given as ``(id, name, is_default)`` in
    creation order: the default board gets ``TODO``, the rest derive in order, each later
    board seeing the keys the earlier ones took. A board whose every suffix is taken gets
    the first free ``B<n>``, since a backfill has nobody to ask."""
    keys: dict[str, str] = {}
    for board_id, name, is_default in boards:
        if is_default:
            keys[board_id] = DEFAULT_BOARD_KEY
            continue
        key = derive_board_key(name, keys.values())
        n = 2
        while key is None:
            candidate = f"B{n}"
            key = candidate if candidate not in keys.values() else None
            n += 1
        keys[board_id] = key
    return keys


def parse_board_key(ref: str) -> str | None:
    """The board key, uppercased, when ``ref`` is shaped like one (any case), else ``None``.

    A board id never is: its prefix carries an underscore, which no key may hold.
    """
    key = ref.strip().upper()
    return key if _BOARD_KEY.fullmatch(key) else None


def parse_todo_key(ref: str) -> tuple[str, int] | None:
    """``(board key, number)`` when ``ref`` is shaped like a to-do key, else ``None``."""
    match = _TODO_KEY.fullmatch(ref.strip())
    if match is None:
        return None
    return match.group(1).upper(), int(match.group(2))


def format_todo_key(board_key: str, number: int) -> str:
    """The displayed key: ``MAG-3``."""
    return f"{board_key.upper()}-{number}"
