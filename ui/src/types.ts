// Wire contracts mirroring the To-dos server's Pydantic models
// and the documented tool return shapes. The UI never sees
// tenant_id/workspace_id — those never cross the tool boundary (models.py).

/** A board column: a presentation overlay mapping a label to a canonical status. */
export interface Column {
  key: string;
  label: string;
  status: string;
  color?: string;
  wip_limit?: number;
}

export interface Board {
  id: string;
  /** The board's key (2-5 of A-Z/0-9), fixed at creation; it prefixes its to-dos' keys. */
  key: string;
  name: string;
  description?: string | null;
  columns?: Column[] | null;
  is_default?: boolean;
  created_at: string;
  updated_at: string;
}

export interface Todo {
  id: string;
  /** The displayed key, `<board key>-<number>` (e.g. `MAG-3`). Every tool that takes a to-do id takes it too. */
  key: string;
  board_id: string;
  parent_id?: string | null;
  title: string;
  details?: string | null;
  status: string;
  priority: string;
  labels?: string[] | null;
  assignee?: string | null;
  order: number;
  due_at?: string | null;
  blocked_reason?: string | null;
  completed_at?: string | null;
  archived_at?: string | null;
  source?: string | null;
  created_at: string;
  updated_at: string;
}

/** A to-do as a row of a page (list_todos, plan_todos, get_todo's steps):
 * `details_preview` in place of `details`. get_todo reads the whole to-do. */
export type TodoRow = Omit<Todo, "details"> & { details_preview?: string | null };

/** The append-only audit row (models.py TodoActivity) — carries no id, no PII. */
export interface TodoActivity {
  todo_id: string;
  action: string;
  outcome: string;
  detail?: Record<string, unknown> | null;
  at: string;
}

// A failed call as the views read it: `error` is the code. On the wire a failure is a tool
// result flagged `isError` whose body is `{error: {code, message, retryable, ...}, next_step}`;
// `useCall` turns it into this with `toApiError`, so a view checks `isApiError` and never
// sees the wire shape.
export interface ApiError {
  error: string;
  message?: string;
  next_step?: string;
  entity?: string;
  id?: string;
}

export interface ListTodosResult {
  todos: TodoRow[];
  next_cursor: string | null;
  next_step?: string;
}

export interface ListBoardsResult {
  boards: Board[];
  next_step?: string;
}

export interface GetTodoResult {
  todo: Todo;
  steps: TodoRow[];
  counts: { steps: number; steps_done: number };
  recent_activity: TodoActivity[];
  next_step?: string;
}

export interface Lane {
  key: string;
  label: string;
  status: string;
  count: number;
  wip_limit?: number;
  wip_ok?: boolean;
}

export interface BoardSummaryResult {
  /** `null` when the workspace has no board yet. */
  board: Board | null;
  lanes: Lane[];
  totals: { active: number; overdue: number; stalled: number; blocked: number };
  attention: { overdue: number; stalled: number; blocked: number };
  next_step?: string;
}

export interface TodoResult {
  todo: Todo;
  next_step?: string;
}

/** A failed tool result's body as an `ApiError`. Takes the server's error body, a body that
 * names its error as a string, or the plain text a host or a schema rejection returns. */
export function toApiError(body: unknown): ApiError {
  if (typeof body === "string") return { error: "error", message: body || undefined };
  if (typeof body === "object" && body !== null) {
    const b = body as { error?: unknown; message?: unknown; next_step?: unknown };
    const nextStep = typeof b.next_step === "string" ? b.next_step : undefined;
    if (typeof b.error === "object" && b.error !== null) {
      const e = b.error as { code?: unknown; message?: unknown; entity?: unknown; id?: unknown };
      return {
        error: typeof e.code === "string" ? e.code : "error",
        message: typeof e.message === "string" ? e.message : undefined,
        next_step: nextStep,
        entity: typeof e.entity === "string" ? e.entity : undefined,
        id: typeof e.id === "string" ? e.id : undefined,
      };
    }
    if (typeof b.error === "string") return body as ApiError;
  }
  return { error: "error", message: "The tool call failed." };
}

export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { error?: unknown }).error === "string"
  );
}
