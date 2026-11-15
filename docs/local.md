# Running locally

`todosforagents` runs the local runtime: everything in one SQLite file on your machine, one
principal, no account and no network beyond loopback.

```sh
todosforagents                # stdio (what an MCP client starts)
todosforagents --http         # HTTP on http://127.0.0.1:8765/mcp
todosforagents --print-token  # print the token HTTP requires, then exit
```

With uv, prefix each with `uvx` (`uvx todosforagents`). `python -m mcp_todos` with
`TODOS_RUNTIME=local` serves stdio too; the MCP bundle starts the server that way.

| Setting | Meaning |
|---|---|
| `TODOS_LOCAL_DB` | The database file. Default: `todos.db` in the per-user data directory |
| `TODOS_LOCAL_PORT` | The port for `--http`. Default: `8765` |
| `TODOS_RUNTIME` | `local`, or unset; `todosforagents` refuses any other value |

## The database file

The default file is `todos.db` in the per-user data directory:

| OS | Directory |
|---|---|
| macOS | `~/Library/Application Support/todosforagents/` |
| Linux | `$XDG_DATA_HOME/todosforagents/`, or `~/.local/share/todosforagents/` |
| Windows | `%LOCALAPPDATA%\todosforagents\` |

The directory and the file are created on first start. Beside the file SQLite keeps
`todos.db-wal` and `todos.db-shm` while the database is open; they are part of it.

## Several processes, one file

Each MCP client starts its own server process, and they may all use the same file. The file is
in WAL mode, so readers never wait for a writer. Every transaction starts with
`BEGIN IMMEDIATE`, taking the write lock up front, so two processes appending to the same lane
take turns and each sees the other's committed work. A process waits up to 15 seconds for the
lock; a call that still cannot get it fails with `rate_limited`, having changed nothing, and is
safe to retry.

**Keep the file on a local disk.** WAL mode does not work on network file systems, and a
synced folder can copy the file mid-write.

## HTTP and its token

`--http` binds to `127.0.0.1` and nowhere else; there is no setting to change that. Every
request is checked twice:

1. **Loopback and `Origin` (403).** A request from a client address that is not loopback, with
   a `Host` header that does not name this machine, or with an `Origin` that is not a loopback
   origin is refused. The `Host` check stops a DNS-rebinding page from reaching the server
   through a browser; the `Origin` check is what the MCP specification asks of a local server.
   A request with no `Origin` (a client that is not a browser) is allowed.
2. **The bearer token (401).** Loopback keeps out other machines and web pages, not other
   programs on this machine, so HTTP also requires `Authorization: Bearer <token>`.

The token lives beside the database, at `<database>.token` (`todos.db.token` by default). It
is made on first use, readable by your user only, and reused on every later start, so a client
configured once keeps working. `todosforagents --print-token` prints it for the database
`TODOS_LOCAL_DB` names, making it first if there is none; an installer can run it before the
server first starts. To change the token, stop the server, delete the `.token` file and start
it again.

stdio needs no token: a process that can start the server can already read the database.

## Upgrades

On start, the server brings the file's schema to the version it was built for:

- A file at the current version is opened as it is.
- A file at an older version is first copied to `<file>.<revision>.bak` (for example
  `todos.db.0001.bak`), then upgraded. The copy goes through SQLite's backup API, so it is
  consistent, WAL contents included.
- A file written by a newer version is refused rather than opened. Upgrade `todosforagents`
  to open it.
- A file that holds tables but no schema version is not a Todos for Agents database, and is
  refused.

Processes starting together upgrade one at a time; each later one finds the file current.

## Backups

To back up while servers may be running, use SQLite's own backup, which is consistent and
includes the WAL:

```sh
sqlite3 "$HOME/Library/Application Support/todosforagents/todos.db" ".backup todos-backup.db"
```

With every server stopped, copying `todos.db` alone is enough. To restore, stop the servers
and put the backup in place of `todos.db`, removing any `todos.db-wal` and `todos.db-shm`
beside it. The `<file>.<revision>.bak` copies an upgrade makes are ordinary database files and
restore the same way, with a version of `todosforagents` that matches them.
