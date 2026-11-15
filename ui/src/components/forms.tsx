import { useCallback, useState } from "react";
import { Button, Stack, Text, tokens } from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { isApiError, type Todo } from "../types";
import { Field, Modal } from "./kit";

type Done = () => void;

const PRIORITY_OPTIONS = [
  { label: "Normal", value: "normal" },
  { label: "Urgent", value: "urgent" },
  { label: "High", value: "high" },
  { label: "Low", value: "low" },
];

function pruneEmpty(obj: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(obj)) {
    const trimmed = value.trim();
    if (trimmed) out[key] = trimmed;
  }
  return out;
}

/** Parse a comma/space-separated label string into a clean string[] (or undefined). */
function parseLabels(raw: string): string[] | undefined {
  const labels = raw
    .split(",")
    .map((l) => l.trim())
    .filter(Boolean);
  return labels.length ? labels : undefined;
}

/** Shared submit lifecycle: call the tool, surface a structured error, or finish. */
export function useToolForm(onDone: Done, onClose: Done) {
  const call = useCall();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async (tool: string, args: Record<string, unknown>) => {
      setBusy(true);
      setError(null);
      try {
        const data = await call<unknown>(tool, args);
        if (isApiError(data)) {
          setError(data.message ?? data.error);
          setBusy(false);
          return;
        }
        onDone();
        onClose();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setBusy(false);
      }
    },
    [call, onDone, onClose],
  );
  return { run, busy, error };
}

export function SubmitFooter({
  busy,
  label,
  onSubmit,
  onClose,
  tone = "primary",
  submitDisabled = false,
}: {
  busy: boolean;
  label: string;
  onSubmit: () => void;
  onClose: Done;
  tone?: "primary" | "danger";
  /** Disables only the primary action (e.g. form invalid). Cancel stays usable. */
  submitDisabled?: boolean;
}) {
  return (
    <>
      <Button variant="ghost" onClick={onClose} disabled={busy}>
        Cancel
      </Button>
      <Button
        variant="primary"
        onClick={onSubmit}
        disabled={busy || submitDisabled}
        style={tone === "danger" ? { background: tokens.danger } : undefined}
      >
        {busy ? "Working…" : label}
      </Button>
    </>
  );
}

export function ErrorText({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <Text size="sm" tone="danger">
      {error}
    </Text>
  );
}

// --------------------------------------------------------------------------- //
// Create to-do                                                                  //
// --------------------------------------------------------------------------- //

export function NewTodoForm({
  boardId,
  status,
  onClose,
  onDone,
}: {
  boardId: string;
  /** Optional starting lane (the board "+" per-lane add). */
  status?: string;
  onClose: Done;
  onDone: Done;
}) {
  const { run, busy, error } = useToolForm(onDone, onClose);
  const [title, setTitle] = useState("");
  const [details, setDetails] = useState("");
  const [priority, setPriority] = useState("normal");
  const [assignee, setAssignee] = useState("");
  const [labels, setLabels] = useState("");
  const [dueAt, setDueAt] = useState("");
  // A to-do added to the blocked lane starts blocked, which needs a reason.
  const [reason, setReason] = useState("");
  const blocked = status === "blocked";

  const submit = () => {
    if (!title.trim() || (blocked && !reason.trim())) return;
    const args: Record<string, unknown> = {
      board_id: boardId,
      ...pruneEmpty({ title, details, priority, assignee }),
    };
    if (status) args.status = status;
    if (blocked) args.blocked_reason = reason.trim();
    const parsed = parseLabels(labels);
    if (parsed) args.labels = parsed;
    if (dueAt.trim()) args.due_at = new Date(dueAt.trim()).toISOString();
    run("create_todo", args);
  };

  return (
    <Modal
      title="New todo"
      onClose={onClose}
      footer={
        <SubmitFooter
          busy={busy}
          label="Add todo"
          submitDisabled={blocked && !reason.trim()}
          onSubmit={submit}
          onClose={onClose}
        />
      }
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Title" value={title} onChange={setTitle} required autoFocus />
        {blocked ? (
          <Field label="Why is it blocked?" value={reason} onChange={setReason} required textarea rows={2} />
        ) : null}
        <Field label="Details" value={details} onChange={setDetails} textarea placeholder="Markdown" />
        <Field label="Priority" value={priority} onChange={setPriority} options={PRIORITY_OPTIONS} />
        <Field label="Assignee" value={assignee} onChange={setAssignee} placeholder="agent / a handle" />
        <Field label="Labels" value={labels} onChange={setLabels} placeholder="comma, separated" />
        <Field label="Due" value={dueAt} onChange={setDueAt} type="datetime-local" />
      </Stack>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Edit to-do                                                                    //
// --------------------------------------------------------------------------- //

export function EditTodoForm({
  todo,
  onClose,
  onDone,
}: {
  todo: Todo;
  onClose: Done;
  onDone: Done;
}) {
  const { run, busy, error } = useToolForm(onDone, onClose);
  const [title, setTitle] = useState(todo.title ?? "");
  const [details, setDetails] = useState(todo.details ?? "");
  const [priority, setPriority] = useState(todo.priority ?? "normal");
  const [assignee, setAssignee] = useState(todo.assignee ?? "");
  const [labels, setLabels] = useState((todo.labels ?? []).join(", "));
  const [dueAt, setDueAt] = useState(toLocalInput(todo.due_at));

  const submit = () => {
    if (!title.trim()) return;
    // update_todo is null-safe partial; details/labels/due cleared to empty are
    // omitted (pruneEmpty / parseLabels), so a blank field leaves them unchanged.
    const args: Record<string, unknown> = {
      todo_id: todo.id,
      ...pruneEmpty({ title, details, priority, assignee }),
    };
    const parsed = parseLabels(labels);
    if (parsed) args.labels = parsed;
    if (dueAt.trim()) args.due_at = new Date(dueAt.trim()).toISOString();
    run("update_todo", args);
  };

  return (
    <Modal
      title="Edit todo"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Save" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Title" value={title} onChange={setTitle} required autoFocus />
        <Field label="Details" value={details} onChange={setDetails} textarea rows={6} />
        <Field label="Priority" value={priority} onChange={setPriority} options={PRIORITY_OPTIONS} />
        <Field label="Assignee" value={assignee} onChange={setAssignee} />
        <Field label="Labels" value={labels} onChange={setLabels} placeholder="comma, separated" />
        <Field label="Due" value={dueAt} onChange={setDueAt} type="datetime-local" />
      </Stack>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Block (the transition that needs a reason: block_todo)             //
// --------------------------------------------------------------------------- //

export function BlockTodoForm({
  todoId,
  onClose,
  onDone,
}: {
  todoId: string;
  onClose: Done;
  onDone: Done;
}) {
  const { run, busy, error } = useToolForm(onDone, onClose);
  const [reason, setReason] = useState("");

  const submit = () => {
    if (!reason.trim()) return;
    run("block_todo", { todo_id: todoId, reason: reason.trim() });
  };

  return (
    <Modal
      title="Block todo"
      onClose={onClose}
      footer={
        <SubmitFooter
          busy={busy}
          label="Block it"
          submitDisabled={!reason.trim()}
          onSubmit={submit}
          onClose={onClose}
          tone="danger"
        />
      }
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Text size="sm" tone="muted">
          A blocked todo records why it stalled. Unblock it later by starting it again.
        </Text>
        <Field label="Reason" value={reason} onChange={setReason} required autoFocus textarea rows={3} />
      </Stack>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Create board                                                                 //
// --------------------------------------------------------------------------- //

export function NewBoardForm({ onClose, onDone }: { onClose: Done; onDone: Done }) {
  const { run, busy, error } = useToolForm(onDone, onClose);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const submit = () => {
    if (!name.trim()) return;
    // Omit columns — the server defaults to the four canonical lanes.
    run("create_board", pruneEmpty({ name, description }));
  };

  return (
    <Modal
      title="New board"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Add board" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Name" value={name} onChange={setName} required autoFocus />
        <Field label="Description" value={description} onChange={setDescription} textarea rows={3} />
        <Text size="xs" tone="faint">
          New boards start with the four canonical lanes: Open · Doing · Blocked · Done.
        </Text>
      </Stack>
    </Modal>
  );
}

/** ISO → value for a <input type="datetime-local"> (local time, no seconds). */
function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
