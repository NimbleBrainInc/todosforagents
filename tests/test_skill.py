"""Usage skill resource — the ``skill://todos/SKILL.md`` guidance overlay.

Mirrors ``test_ui.py``: importing ``mcp_todos.server`` touches no database (the
engine is lazy), so these lock the skill resource's registration and content
without a database. The skill is bundled inside the package and pointed
at by the server instructions, so a connecting agent loads tool-selection
guidance (status moves via the transition verbs, never ``update_todo``).
"""

import asyncio

import pytest
import yaml
from nimblebrain_server_sdk.testing import Mode, served_skills

from mcp_todos.server import SKILL_CONTENT, mcp


def test_skill_resource_registered_as_markdown():
    resource = asyncio.run(mcp.get_resource("skill://todos/SKILL.md"))
    assert resource is not None, "skill://todos/SKILL.md resource is not registered"
    assert str(resource.uri) == "skill://todos/SKILL.md"
    assert resource.mime_type == "text/markdown"


def test_skill_resource_serves_the_bundled_guidance():
    result = asyncio.run(mcp.read_resource("skill://todos/SKILL.md"))
    body = result.contents[0].content
    assert isinstance(body, str)
    assert body == SKILL_CONTENT
    # The load-bearing guidance: status only moves via the transition verbs.
    assert "`update_todo` has no status" in body
    for verb in ("start_todo", "complete_todo", "block_todo", "reopen_todo", "cancel_todo"):
        assert verb in body
    assert "plan_todos" in body
    # First use: a short plan on one board, areas as labels, keys told to the person.
    first_use = body.split("## First use", 1)[1].split("\n## ", 1)[0]
    for guidance in ("plan_todos", "labels", "keys"):
        assert guidance in first_use


def test_server_instructions_point_at_the_skill():
    # Without the pointer in the instructions the resource never gets read.
    assert mcp.instructions is not None
    assert "skill://todos/SKILL.md" in mcp.instructions


def test_skill_frontmatter_parses_with_name_and_description():
    """SKILL.md frontmatter must be valid Agent-Skills YAML (name + description).

    Guards the unquoted colon-space footgun that makes js-yaml (the runtime) / PyYAML
    throw ``mapping values are not allowed here`` — which the host degrades to a dropped
    description plus the raw ``---`` fence leaking into the injected skill body.
    """
    assert SKILL_CONTENT.startswith("---")
    _, frontmatter, _body = SKILL_CONTENT.split("---", 2)
    meta = yaml.safe_load(frontmatter)
    assert meta["name"] == "todos"
    assert isinstance(meta.get("description"), str) and meta["description"].strip()


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_the_skills_extension_publishes_the_skill(mode: Mode):
    """skills/list names exactly this server's skill, and every listed file reads back through
    resources/read with the listed digest; skills/get answers with the same entry. On
    2026-07-28 the server declares io.modelcontextprotocol/skills (`served_skills` checks it).
    """
    entries = await served_skills(mcp, mode)

    assert list(entries) == ["skill://todos/SKILL.md"]
    entry = entries["skill://todos/SKILL.md"]
    assert [r["uri"] for r in entry["resources"]] == ["skill://todos/SKILL.md"]
    assert entry["frontmatter"] == yaml.safe_load(SKILL_CONTENT.split("---", 2)[1])
