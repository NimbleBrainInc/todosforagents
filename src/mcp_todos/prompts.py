"""Prompts: the person's own way into a flow the agent otherwise reaches through tool results.

``get_started`` asks for the first-use plan in the terms the empty reads' ``next_step`` and the
skill's "First use" section already give the agent: 3 to 5 concrete to-dos, made with
``plan_todos``, reported by key.

This module has no ``from __future__ import annotations``: FastMCP reads a prompt argument's
annotation as written, and a string ``"str"`` makes it append a JSON-schema note to the
argument's description, which hosts show to the person.
"""


def get_started(focus: str = "") -> str:
    """Start the person's to-do list, or pick it up.

    Args:
        focus: What you are working on. Leave it empty to start from your board.
    """
    focus = focus.strip()
    if focus:
        return (
            f"Help me get started with my to-dos. I am working on: {focus}\n\n"
            "Propose 3 to 5 concrete to-dos for it. Once I agree, create them with plan_todos, "
            "then tell me the keys they got."
        )
    return (
        "Help me get started with my to-dos. First check my board with get_board_summary.\n\n"
        "If there are no to-dos yet, ask me what I am working on, then propose 3 to 5 concrete "
        "to-dos. Once I agree, create them with plan_todos, then tell me the keys they got.\n\n"
        "If there are to-dos, tell me what is in progress and what is next (get_next_todo), "
        "naming each to-do by its key."
    )
