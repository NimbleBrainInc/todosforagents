# Architecture

Todos for Agents is one Python distribution, `todosforagents`, with two import packages:

| Package | Holds |
|---|---|
| `mcp_todos` | The core: wire models, ids and keys, tables, the store, the 17 tools, the skill, the board UI resource, and the runtime contract |
| `mcp_todos_local` | The local runtime: one SQLite file, one principal, stdio and loopback HTTP, and its own schema history |

The core is the same wherever the server runs. What differs between deployments is a small set
of implementations, a **runtime**.

## Runtimes

A runtime implements the `Runtime` protocol in `mcp_todos/runtime.py`:

| Member | Answers |
|---|---|
| `identity()` | Who is calling. Returns a `Scope` (`tenant_id`, `workspace_id`, optional `user_id` and `correlation_id`), or raises `AuthError` when no verified identity exists |
| `auth()` | The FastMCP auth provider that gates the HTTP transport, or `None`. A request without a token it verifies gets 401 with a `WWW-Authenticate` challenge before any tool runs, and `identity()` reads the verified token through FastMCP's `get_access_token()` |
| `session(scope)` | One database transaction for that scope. May raise `Overloaded` on entry, when it has no capacity for the call |
| `limits(scope)` | A `Limits` (`max_boards`, `max_open_todos`) the store enforces for that scope's tenant, or `None` for no limits |
| `record(event, count)` | What to do with a domain event (created, completed, blocked, a denied lookup): count it, log it, or ignore it |
| `attach(mcp)` | What the server serves beyond the core's tools and resources, such as routes or extra resources |
| `lifespan()` | Startup and shutdown work, such as bringing the schema to head |
| `wrap_http(app)` | The HTTP app as this runtime serves it: the middleware in front of the MCP endpoint |

Every tool does the same three things: it asks the runtime for the caller's identity, opens one
transaction through the runtime for that identity, and calls the store. **Identity never comes
from a tool argument**, and no result carries `tenant_id` or `workspace_id`.

The runtime is chosen after `server.py` builds `mcp`, so its auth provider cannot go to the
`FastMCP` constructor. `install()` sets `mcp.auth` from `auth()` instead: FastMCP reads that
attribute when `http_app()` builds the Starlette app, and the app is built only after the
runtime is installed. The local runtime returns `None`; it guards HTTP with its own loopback
checks and bearer token in `wrap_http`.

### Limits

When `limits(scope)` returns `Limits`, the store counts inside the write's own transaction,
after the change, and refuses with `invalid_request` when the tenant is over: a board past
`max_boards` (the auto-created default board included), or an **open** to-do past
`max_open_todos`. An open to-do is one that is neither `done` nor `cancelled` and is not
archived, so completing, cancelling or archiving frees room, and any move that makes a finished
to-do open again (`reopen_todo`, `start_todo`, `block_todo`) counts. An idempotent replay adds
nothing and is never refused. The count runs under the caller's session, so row-level security
still applies, and after a transaction-scoped lock on the tenant (an advisory lock on Postgres;
SQLite's `BEGIN IMMEDIATE` already serializes writers), so concurrent writes cannot both take
the last slot.

Runtimes register under the `mcp_todos.runtimes` entry-point group, and `TODOS_RUNTIME` names
the one to use. The generic entry point, `python -m mcp_todos`, refuses to start when
`TODOS_RUNTIME` is unset or names no installed runtime: a multi-principal deployment that fell
back to a single-principal runtime would drop its isolation, so nothing guesses.
`todosforagents` is the local runtime's own command, so it selects `local` itself.

### Why the local runtime has one principal

The local runtime's `identity()` always returns `Scope("local", "local")`, whatever the request
carries. Isolation is by installation: the database is a file in your user directory, so
whoever can reach the server can already read the file. With one principal, no query needs a
tenant predicate, and SQLite, which has no row-level security, is safe to use.

The tables still carry `tenant_id` and `workspace_id` on every row, so the same store serves a
runtime with many principals. Such a runtime needs a database that enforces isolation itself;
on Postgres the core's tables are designed for row-level security keyed on those columns.

## Data model

Three tables, defined once in `mcp_todos/db/tables.py`:

| Table | Holds |
|---|---|
| `board` | `id` (`bd_<ulid>`), `key` (`MAG`), name, description, `columns`, `is_default`, `next_number` |
| `todo` | `id` (`td_<ulid>`), `board_id`, `number`, optional `parent_id`, title, details, status, priority, labels, assignee, sort position, due, blocked reason, completion and archive times, source, idempotency key |
| `todo_activity` | One append-only row per change and per denied or failed attempt: the to-do, the action, the outcome, and the names (never the values) of the fields it set |

- **Ids are opaque and stable.** `bd_` and `td_` followed by a ULID, minted in order, so a
  plan's steps sort as they were given.
- **Keys are for people.** A board's key is 2 to 5 letters or digits, fixed at creation. A
  to-do's key is `<board key>-<number>`, numbered per board and never reused. Every id
  argument accepts the key.
- **Status is the contract.** `open`, `in_progress`, `blocked`, `done`, `cancelled`. Each move
  is its own verb, which owns its side effect (`completed_at`, `blocked_reason`). A board's
  `columns` are a presentation overlay: each lane shows one status, and editing them never
  changes a to-do.
- **Strings stay open.** Status is checked by the store, not by a database constraint;
  priority, assignee and source are free strings, and an unknown priority sorts last.
- **Creates converge.** With an `idempotency_key`, a retried `create_todo` or `plan_todos`
  returns the same rows instead of adding them again, under a partial unique index.
- **Paging is keyset.** `list_todos` returns `next_cursor`; a cursor encodes a position, not an
  offset, and the cursors issued today are frozen by `tests/golden/paging.txt`.

## The board UI and MCP Apps

The board is a React app in `ui/`, built into one self-contained HTML file and shipped inside
the wheel. The server serves it as `ui://todos/main` with the MIME type
`text/html;profile=mcp-app`. Serving it reads a file: it carries no data and opens no database
session. Everything the board shows, it fetches through the same tools an agent calls, by
way of the host.

Two tools are bound to the UI: `get_board_summary`, so a host shows the board when an agent
reads its health, and `get_todo`, so it shows that to-do. Both are visible to the model and to
the app.

**The bound URI is keyed by the build.** Tools bind to `ui://todos/main/<build>`, where
`<build>` is the first 12 hex characters of the SHA-256 of the served HTML. A host may cache a
`ui://` resource by its URI; with the hash in the URI, a new UI is a new URI, and a host fetches
it instead of rendering a stale copy. Any key resolves to the current UI, so a conversation or
tool list that names an older build still renders. The listed resource stays
`ui://todos/main`, so a host placement that names it keeps working across builds.

ChatGPT reads two keys of its own beside the MCP Apps ones, both defined in the OpenAI Apps
SDK reference (<https://developers.openai.com/plugins/reference>). Every tool carries
`openai/widgetAccessible: true`, so the board can call the tools it uses from its frame. The
board resource and its build template carry `openai/widgetDomain:
"https://todosforagents.com"`, ChatGPT's alias for `ui.domain`; the alias holds it because other
hosts check `ui.domain` against domains of their own.

The frame's content security policy allows no outside origin, since the board loads nothing
from elsewhere. Markdown in a to-do's details is sanitized before it is rendered.

## Errors

A failure is a tool result flagged `isError`, with one body:

```json
{"error": {"code": "not_found", "message": "...", "retryable": false}, "next_step": "..."}
```

| Code | When |
|---|---|
| `not_found` | The id or key names nothing the caller can see. An absent record and one outside the caller's scope are indistinguishable |
| `invalid_request` | An argument is wrong, or the write would pass one of the runtime's limits; nothing was changed. A limit refusal carries `limit` (`max_boards` or `max_open_todos`) in the envelope |
| `unauthenticated` | The runtime established no identity. Returned before any database session opens |
| `rate_limited` | The runtime had no capacity for the call and ran nothing; a retry is safe |
| `internal_error` | Anything else. The message is fixed and generic; the details go to the server log |

A denied or failed attempt is also recorded in `todo_activity`, in a transaction of its own.

## Annotations

Every tool is annotated, since the MCP specification treats an unannotated tool as
destructive. All are closed-world: they act on this server's database and nothing else.

| Kind | Tools | Read-only | Destructive | Idempotent |
|---|---|---|---|---|
| Read | `list_todos`, `get_todo`, `get_next_todo`, `get_board_summary`, `list_boards` | yes | (n/a) | yes |
| Add | `create_todo`, `plan_todos`, `create_board` | no | no | no |
| Edit | `update_todo`, `reorder_todos`, `update_board` | no | yes | yes |
| Move | `start_todo`, `block_todo`, `reopen_todo` | no | no | yes |
| Move, stamped | `complete_todo`, `cancel_todo` | no | no | no |
| Hide | `archive_todo` | no | yes | no |

A field edit overwrites values, so it is destructive; a status move is undone by another verb,
so it is not. `complete_todo` and `cancel_todo` stamp the time of the call, so a second call
changes the to-do again. A committed write sends `notifications/resources/list_changed`, so an
open board refreshes; a read never does.

## The surface is frozen

`tests/golden/surface.json` holds what a client sees: every tool with its schemas and
annotations, the resources and templates, the capabilities, the instructions, and a digest of
the skill. The in-process server and a separate `todosforagents` process over stdio must both
match it. A deliberate change edits the golden file in the same pull request, so review sees
the diff.
