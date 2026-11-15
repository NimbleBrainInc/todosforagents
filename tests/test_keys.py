"""Board keys and to-do keys, the pure rules (:mod:`mcp_todos.keys`)."""

from __future__ import annotations

import pytest

from mcp_todos.keys import (
    DEFAULT_BOARD_KEY,
    assign_board_keys,
    derive_board_key,
    format_todo_key,
    normalize_board_key,
    parse_todo_key,
)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Magnificent board", "MAG"),
        ("Launch", "LAU"),
        ("Launch plan", "LP"),
        ("Q4 marketing campaign", "QMC"),
        # Generic words drop out, case-insensitively, wherever they sit.
        ("The Board of Launch", "LAU"),
        ("My list for the Launch and the Plan", "LP"),
        # Initials stop at five.
        ("alpha beta gamma delta epsilon zeta", "ABGDE"),
        # Each word keeps its letters and digits only.
        ("Re-launch (v2)!", "RV"),
        ("R&D roadmap", "RR"),
        # A digit first gets a B in front, trimmed to five.
        ("2024 roadmap", "B2R"),
        ("12345", "B123"),
        ("1 2 3 4 5", "B1234"),
        # Shorter than two characters, or nothing left: TB.
        ("X", "TB"),
        ("x board", "TB"),
        ("My board", "TB"),
        ("The list", "TB"),
        ("", "TB"),
        ("!!!", "TB"),
        ("Go", "GO"),
    ],
)
def test_a_key_derives_from_the_name(name: str, key: str) -> None:
    assert derive_board_key(name, []) == key


def test_a_clash_takes_the_lowest_free_digit() -> None:
    assert derive_board_key("Magnificent", ["MAG"]) == "MAG2"
    assert derive_board_key("Magnificent", ["MAG", "MAG2", "MAG3"]) == "MAG4"
    assert derive_board_key("Magnificent", ["mag"]) == "MAG2"  # taken keys compare uppercased


def test_a_clash_trims_the_base_so_the_key_stays_within_five() -> None:
    name = "alpha beta gamma delta epsilon"
    assert derive_board_key(name, ["ABGDE"]) == "ABGD2"
    assert derive_board_key(name, ["ABGDE", "ABGD2"]) == "ABGD3"


def test_every_suffix_taken_asks_for_an_explicit_key() -> None:
    taken = ["LAU"] + [f"LAU{d}" for d in range(2, 10)]
    assert derive_board_key("Launch", taken) is None


def test_the_default_boards_key_is_never_derived_for_another_board() -> None:
    assert derive_board_key("Tom Owen Dan Oscar", []) == "TODO2"
    assert derive_board_key("Todo", []) == "TOD"


@pytest.mark.parametrize("given", ["MAG", "mag", " Mag ", "AB", "ABCDE", "A1", "Q4X"])
def test_an_explicit_key_is_uppercased(given: str) -> None:
    assert normalize_board_key(given) == given.strip().upper()


@pytest.mark.parametrize("given", ["", "A", "ABCDEF", "1AB", "A-B", "A B", "MÄG", "A_1"])
def test_an_explicit_key_that_breaks_the_rule_is_refused(given: str) -> None:
    with pytest.raises(ValueError, match="2-5 characters"):
        normalize_board_key(given)


def test_existing_boards_take_keys_in_creation_order() -> None:
    keys = assign_board_keys(
        [
            ("b1", "Launch", False),
            ("b2", "To-dos", True),
            ("b3", "Launch again", False),  # LA
            ("b4", "Launch", False),  # LAU taken by b1
            ("b5", "Tom Owen Dan Oscar", False),  # TODO is the default board's
        ]
    )
    assert keys == {"b1": "LAU", "b2": DEFAULT_BOARD_KEY, "b3": "LA", "b4": "LAU2", "b5": "TODO2"}


def test_a_backfilled_board_with_no_free_suffix_still_gets_a_key() -> None:
    boards = [(f"b{i}", "Launch", False) for i in range(10)]
    keys = assign_board_keys(boards)
    assert keys["b0"] == "LAU"
    assert keys["b8"] == "LAU9"
    assert keys["b9"] == "B2"
    assert len(set(keys.values())) == len(keys)


@pytest.mark.parametrize(
    ("ref", "parsed"),
    [
        ("MAG-3", ("MAG", 3)),
        ("mag-3", ("MAG", 3)),
        (" Mag-03 ", ("MAG", 3)),
        ("TODO2-41", ("TODO2", 41)),
        ("td_01JNQ8X5Z3K7M2P4R6T8V0W2Y4", None),
        ("tk_01JNQ8X5Z3K7M2P4R6T8V0W2Y4", None),
        ("MAG", None),
        ("MAG-", None),
        ("3-MAG", None),
        ("ABCDEF-1", None),
        ("MAG-1234567890", None),  # past nine digits it cannot name a stored number
    ],
)
def test_a_reference_parses_as_a_key_only_when_shaped_like_one(
    ref: str, parsed: tuple[str, int] | None
) -> None:
    assert parse_todo_key(ref) == parsed


def test_a_key_renders_uppercase() -> None:
    assert format_todo_key("mag", 3) == "MAG-3"
