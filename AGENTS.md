# Todos for Agents

To-do boards for AI agents, as an MCP server with a board UI. One distribution,
`todosforagents`, holds the core (`src/mcp_todos/`) and the local runtime
(`src/mcp_todos_local/`); the board UI is built from `ui/` into the wheel.
`docs/architecture.md` explains the design; `docs/local.md` the local runtime.

## This repository is public

Every file, commit message, pull request, issue and comment here is public and indexed. Never
name an internal host, cloud account, registry path, private repository or package, internal
service, or customer, not even as an example; use `acme-corp`, `tenant-a`,
`example.com`. Decode any base64 in a fixture before committing it, and check the full
diff and commit message for these before pushing.

## Rules the code holds

- **Identity comes from the runtime, never from a tool argument.** A tool asks the runtime for
  the caller's `Scope`, then opens a session for it. No result returns `tenant_id` or
  `workspace_id`.
- **The core names no runtime.** Runtimes register under the `mcp_todos.runtimes` entry-point
  group; `TODOS_RUNTIME` selects one, and `python -m mcp_todos` refuses to start without a
  valid one. `todosforagents` is the local runtime's own command and selects it.
- **The local runtime has one principal.** It is safe on SQLite only because of that. Do not
  add a second principal to it.
- **No Postgres driver, no SDK extras.** The package imports no `asyncpg` and no
  `nimblebrain_server_sdk` `[db]` or `[observability]` module (`tests/test_import_boundary.py`).
- **Every tool is annotated** as a read, an add, an edit, a move or a hide, and is
  closed-world. An unannotated tool is treated as destructive.
- **Status moves only through the transition verbs.** `update_todo` never changes status.
- **Writes announce, reads don't.** A committed write sends `resources/list_changed`.
- **Paging is keyset**, owned by the store through `pgkeyset` (pinned exactly).
- **A table change needs a migration** in `src/mcp_todos_local/migrations/`;
  `tests/test_schema_parity.py` holds the history to `mcp_todos.db.tables.metadata`.

## Golden files

`tests/golden/` holds frozen output with no regenerate path:

| File | Holds |
|---|---|
| `surface.json` | Every tool with its schemas and annotations, every prompt with its arguments, the resources and templates, the capabilities, the instructions, and a digest of the skill, as the local runtime serves them. The UI build hash in `ui://todos/main/<build>` is normalized to `<build>` |
| `paging.txt` | The cursors agents already hold, and the SQL each resumes with |

A deliberate change edits the golden file by hand in the same pull request, so review sees the
diff. A dependency bump that changes one is a reviewed change too: that is why
`nimblebrain-server-sdk` and `pgkeyset` are pinned exactly and SQLAlchemy is held below 2.1.

## Working here

- `make verify` runs everything CI runs: format, lint, ty, the UI build, every Python suite,
  the wheel check, and the UI typecheck and tests. It passes with no skips. The wheel test
  skips without a built UI, but `make verify` builds it first, and in CI (`CI` set) a skip
  fails instead.
- Python tooling is uv, ruff and ty. Suppress a ty diagnostic with `# ty: ignore[rule]`.
- The UI needs Node 24 (`cd ui && npm ci`).
- `tests/local/` drives the server as a separate process over stdio and HTTP, the way a
  client sees it. Keep a behavior test there when it depends on process boundaries.
- Every change lands by pull request with the `verify` check green.
- Comments and docs describe the current state and why it is that way; what changed belongs in
  git history.
