// What the host opened this view for. An MCP Apps host renders the view for the tool bound to
// it: `get_board_summary` opens the board, `get_todo` one to-do's detail. The host names the tool
// in `toolInfo` and delivers the call's input and result; any of the three may be missing.

/** The to-do a `get_todo` call opened the view on, and the board it lives on when known. */
export interface DetailTarget {
  todoId: string;
  boardId?: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

/** The to-do a `get_todo` result carries: `{todo, steps, ...}` with no `error`. */
function fromResult(result: unknown): DetailTarget | null {
  const body = record(result);
  const todo = record(body?.todo);
  if (!body || body.error !== undefined || !todo || !Array.isArray(body.steps)) return null;
  if (typeof todo.id !== "string") return null;
  return {
    todoId: todo.id,
    boardId: typeof todo.board_id === "string" ? todo.board_id : undefined,
  };
}

/**
 * The detail to open, or `null` to show the board as usual: the view was opened by
 * `get_board_summary`, by no tool at all, or by a `get_todo` call that named nothing readable.
 * A tool name the host leaves out falls back to the shape of the result.
 */
export function detailTarget(
  toolName: string | undefined,
  input: Record<string, unknown> | null,
  structuredResult: unknown,
): DetailTarget | null {
  if (toolName !== undefined && toolName !== "get_todo") return null;
  const target = fromResult(structuredResult);
  if (target) return target;
  // The result has not arrived (or was an error): the input names the to-do, by id or key,
  // and the detail reads it itself.
  if (toolName === "get_todo" && typeof input?.todo_id === "string" && input.todo_id.trim()) {
    return { todoId: input.todo_id.trim() };
  }
  return null;
}
