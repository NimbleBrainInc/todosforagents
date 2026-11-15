---
name: todos
description: Manage work on a board. Create and decompose to-dos, move them through the fixed status lifecycle, and read board state.
metadata:
  nimblebrain:
    loading-strategy: dynamic
    tool-affinity:
      - create_todo
      - plan_todos
      - update_todo
      - list_todos
      - get_todo
      - get_next_todo
      - get_board_summary
      - archive_todo
---

# To-dos

Every tool returns a `next_step`; follow it.

## Status moves only through the verb that names what happened

`update_todo` has no status. The five states (`open · in_progress · blocked · done ·
cancelled`) are a set, not a pipeline, and each move is its own verb, which records its own
side effects:

| What happened | Verb | Records |
|---|---|---|
| work began, or its blocker cleared | `start_todo` | clears any block |
| it is finished | `complete_todo` | `completed_at` |
| it is waiting on something | `block_todo` with a reason | `blocked_reason` |
| it is back to do | `reopen_todo` | clears `completed_at` and any block |
| it will not be done | `cancel_todo` (takes no reason) | `completed_at` |

A to-do can also start in any status: `create_todo` and `plan_todos` take `status`, with
the same side effects (`done` and `cancelled` set `completed_at`; `blocked` needs a
`blocked_reason`).

`update_todo` changes everything else: title, details, priority, labels, assignee, due
date, parent.

**Finished, won't do and out of sight are three different things.** `complete_todo` is
finished, `cancel_todo` is won't do, and `archive_todo` only hides a to-do from default
views with its status unchanged, and nothing un-archives it. Never archive to mean done.

## Choosing the rest

- One to-do: `create_todo`. Several, or one goal broken into steps: `plan_todos`, which
  adds to the board and never replaces it. Pass an `idempotency_key` so a retry converges.
- A result flagged as an error carries `error.code` (`not_found`, `invalid_request`,
  `unauthenticated`, `rate_limited`, `internal_error`) and a `next_step`; follow it.
- "What should I do next": `get_next_todo`, rather than listing and re-deciding.
- Reporting on the board: start with `get_board_summary`, then name the to-dos behind its
  counts (what is in progress, what is blocked and why) rather than the counts alone.

## Naming a to-do

Every to-do has a key, its board's key and its number on that board (`MAG-3`). Name to-dos
to the person by key. Every argument that takes a to-do id takes the key too, so when the
person says "MAG-3", pass it as it is. Every `board_id` takes the board's key (`MAG`) too.
`create_board` picks a board's key, or takes one you pass; it cannot change later, so tell
the person which key the board got.
