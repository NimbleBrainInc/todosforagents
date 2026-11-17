# Todos for Agents

To-do boards for AI agents, served as an MCP server. An agent lays out a plan, moves each
to-do through its status, records why one is blocked, and asks what to do next. A person
sees the same boards in a board UI that renders inside MCP Apps hosts.

It runs on your own machine: one SQLite file, no account, no cloud service.

## Install

Run it with [uv](https://docs.astral.sh/uv/) (Python 3.13 or later):

```sh
uvx todosforagents
```

That serves MCP over stdio. To add it to Claude Code:

```sh
claude mcp add todosforagents -- uvx todosforagents
```

For Claude Desktop, the MCP bundle `@nimblebraininc/todosforagents` is available from
[mpak](https://www.mpak.dev). It runs the same package and asks where to keep the database.

Any other MCP client that starts a stdio server can run `uvx todosforagents` the same way.
To serve HTTP on `127.0.0.1` instead, run `uvx todosforagents --http`; HTTP requires a
bearer token, which `uvx todosforagents --print-token` prints. See
[`docs/local.md`](docs/local.md).

## Where your data lives

Everything is in one SQLite file, `todos.db`, in your user data directory:

| OS | Default location |
|---|---|
| macOS | `~/Library/Application Support/todosforagents/todos.db` |
| Linux | `$XDG_DATA_HOME/todosforagents/todos.db` (`~/.local/share/...` when unset) |
| Windows | `%LOCALAPPDATA%\todosforagents\todos.db` |

Set `TODOS_LOCAL_DB` to use another file. Keep it on a local disk, not a network share.
Several clients may use the same file at once. Upgrades, backups and the HTTP token are
covered in [`docs/local.md`](docs/local.md).

## Tools

| Tool | What it does |
|---|---|
| `create_todo` | Add one to-do (to the default board unless one is named) |
| `plan_todos` | Add several to-dos in one call, optionally as the steps of a goal |
| `update_todo` | Change a to-do's title, details, priority, labels, assignee, due date or goal |
| `start_todo` | Mark a to-do in progress |
| `complete_todo` | Mark a to-do done |
| `block_todo` | Mark a to-do blocked, with the reason |
| `reopen_todo` | Put a to-do back to open |
| `cancel_todo` | Mark a to-do as won't do |
| `reorder_todos` | Set the order of one status lane on a board |
| `archive_todo` | Hide a to-do from default views, status unchanged |
| `list_todos` | List or search to-dos, with filters and cursor paging |
| `get_todo` | Read one to-do whole, with its steps and recent activity |
| `get_next_todo` | The highest-priority open to-do: what to work on next |
| `get_board_summary` | A board's lane counts and what needs attention |
| `list_boards` | List the boards |
| `create_board` | Create a board |
| `update_board` | Change a board's name, description or lanes |

A to-do's status (`open`, `in_progress`, `blocked`, `done`, `cancelled`) changes only through
the five status verbs. Every result carries a `next_step` telling the agent what to do with
it, and the server also serves a skill (`skill://todos/SKILL.md`) with the rules for choosing
a tool.

## Keys

Every board has a short key, and every to-do a key made of its board's key and its number
on that board: `MAG-3` is the third to-do on board `MAG`. A to-do that names no board goes on
the workspace's default board: the first board created, or one made with the key `TODO` if
there is none. Any argument that takes a to-do id takes its key, and any argument that takes a board id
takes the board's key, so an agent can pass on "MAG-3" exactly as a person said it.

## The board UI

`get_board_summary` and `get_todo` are bound to the board UI (`ui://todos/main`). A host that
supports [MCP Apps](https://modelcontextprotocol.io/) shows the board, or the to-do, when the
agent calls them. The board reads and changes data only through the same tools.

## More

- [`docs/architecture.md`](docs/architecture.md): the core, runtimes, data model, the UI
  binding, errors and annotations.
- [`docs/local.md`](docs/local.md): the database file, several processes, the HTTP token,
  upgrades and backups.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): building and testing.

## Licence

MIT. See [`LICENSE`](LICENSE).
