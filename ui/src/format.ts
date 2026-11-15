import DOMPurify from "dompurify";
import { marked } from "marked";
import type { TodoActivity } from "./types";

/**
 * Render a to-do's `details` markdown to sanitized HTML for `<Prose html>`.
 *
 * `details` is agent- or user-authored markdown, untrusted when rendered. It reaches
 * the DOM through `Prose`'s `dangerouslySetInnerHTML`, and the host iframe runs
 * with `allow-scripts` + a `script-src 'unsafe-inline'` CSP (required by the
 * single-file bundle), so an injected `<script>`/`onerror` would execute **with
 * full tool-bridge authority**. The result is therefore run through DOMPurify
 * between `marked` and `Prose` — sanitization here is the load-bearing defense,
 * not the CSP.
 */
export function markdownToHtml(md: string): string {
  const html = marked.parse(md, { async: false }) as string;
  return DOMPurify.sanitize(html);
}

/** "Jun 19, 2026" — date only. */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** "Jun 19, 2026, 2:30 PM" — date + time, for activity timestamps. */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** True when a due date is in the past (and the to-do isn't terminal). */
export function isOverdue(due: string | null | undefined): boolean {
  if (!due) return false;
  const d = new Date(due);
  if (Number.isNaN(d.getTime())) return false;
  return d.getTime() < Date.now();
}

/** Sentence-case word for a priority (urgent / high / normal / low). */
export function priorityLabel(priority: string): string {
  if (!priority) return "Normal";
  return priority.charAt(0).toUpperCase() + priority.slice(1);
}

/** Label dot colours: mid-tones that hold 4.5:1 or better against a white surface. */
const LABEL_COLORS = [
  "#2B5FBF",
  "#0F7B6C",
  "#7A5AC8",
  "#B4540A",
  "#C2417A",
  "#4D7C0F",
  "#0E7490",
  "#A16207",
];

/** A label's dot colour: the same name always gets the same colour. */
export function labelColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  return LABEL_COLORS[Math.abs(hash) % LABEL_COLORS.length];
}

/** A clock time in the viewer's locale: "2:55 PM". */
export function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** The heading for a day of activity, in the viewer's local time: "Today", "Yesterday",
 * the date this year ("Nov 13"), otherwise the date with its year. */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  return fmtDate(iso);
}

/** How many activity rows get_todo returns at most (the store's recent-activity cap). Fewer
 * than this means the to-do's whole history is here. */
export const RECENT_ACTIVITY_CAP = 20;

/** The status each transition action moves a to-do into. */
const TRANSITION_TO: Record<string, string> = {
  start: "in_progress",
  complete: "done",
  block: "blocked",
  reopen: "open",
  cancel: "cancelled",
};

/** How an edited field is named in a sentence. A field not listed reads as its own name. */
const FIELD_WORDS: Record<string, string> = {
  title: "the title",
  details: "details",
  priority: "the priority",
  labels: "labels",
  assignee: "the assignee",
  due_at: "the due date",
  parent_id: "the goal",
};

/** Join words as a sentence does: "a", "a and b", "a, b and c". */
export function joinNatural(words: string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/** An edit as a sentence, naming the fields `detail.fields` lists: "Edited the title and
 * details". A row that names none reads "Edited". */
export function editedSentence(detail: Record<string, unknown> | null | undefined): string {
  const fields = Array.isArray(detail?.fields)
    ? detail.fields.filter((f): f is string => typeof f === "string" && f.length > 0)
    : [];
  if (!fields.length) return "Edited";
  return `Edited ${joinNatural(fields.map((f) => FIELD_WORDS[f] ?? f.replace(/_/g, " ")))}`;
}

/** The mark an activity row draws on the rail. */
export type ActivityNode =
  | { kind: "status"; status: string }
  | { kind: "edit" }
  | { kind: "create" }
  | { kind: "plain" };

/** One row of a to-do's history: a sentence (`text`, then `emphasis` set apart), an optional
 * muted second line, and the rail mark. */
export interface ActivityEntry {
  at: string;
  node: ActivityNode;
  text: string;
  emphasis?: string;
  note?: string;
}

/**
 * A to-do's history from get_todo's `recent_activity` (newest first), one sentence per change.
 * Only rows with outcome `ok` are history: a denied or failed attempt changed nothing and is
 * the audit trail's concern. Rows that record no change are left out too: a create that matched
 * an existing to-do by its idempotency key, a plan that created nothing, and a reorder (a
 * lane position, recorded against the board). The rows carry no actor the UI can name, so no
 * sentence names one.
 *
 * A to-do laid down by plan_todos has no create row of its own (the plan's row goes on its
 * goal or board), so when the whole history is here and holds no creation, `createdAt` adds one.
 */
export function activityEntries(
  activity: TodoActivity[],
  { board, createdAt }: { board?: string; createdAt?: string } = {},
): ActivityEntry[] {
  const created = (at: string): ActivityEntry =>
    board
      ? { at, node: { kind: "create" }, text: "Created on ", emphasis: board }
      : { at, node: { kind: "create" }, text: "Created" };
  const out: ActivityEntry[] = [];
  let sawCreate = false;
  for (const a of activity) {
    if (a.outcome !== "ok" || a.action === "reorder") continue;
    const to = TRANSITION_TO[a.action];
    if (to) {
      out.push({ at: a.at, node: { kind: "status", status: to }, text: "Moved to ", emphasis: statusLabel(to) });
    } else if (a.action === "update") {
      out.push({ at: a.at, node: { kind: "edit" }, text: editedSentence(a.detail) });
    } else if (a.action === "create") {
      if (a.detail?.matched) continue;
      sawCreate = true;
      out.push(created(a.at));
    } else if (a.action === "plan") {
      const made = Number(a.detail?.created ?? 0);
      const items = Number(a.detail?.items ?? made);
      if (!made) continue;
      out.push({
        at: a.at,
        node: { kind: "create" },
        text: `Added ${made} ${made === 1 ? "step" : "steps"}`,
        note: `from a plan of ${items} ${items === 1 ? "to-do" : "to-dos"}`,
      });
    } else if (a.action === "archive") {
      out.push({ at: a.at, node: { kind: "plain" }, text: "Archived" });
    } else {
      const verb = a.action.replace(/_/g, " ");
      out.push({ at: a.at, node: { kind: "plain" }, text: verb.charAt(0).toUpperCase() + verb.slice(1) });
    }
  }
  if (!sawCreate && createdAt && activity.length < RECENT_ACTIVITY_CAP) {
    out.push(created(createdAt));
  }
  return out;
}

/** Entries (newest first) grouped under their day's heading, in order. */
export function groupByDay(
  entries: ActivityEntry[],
  now: Date = new Date(),
): { day: string; entries: ActivityEntry[] }[] {
  const groups: { day: string; entries: ActivityEntry[] }[] = [];
  for (const e of entries) {
    const day = dayLabel(e.at, now);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.entries.push(e);
    else groups.push({ day, entries: [e] });
  }
  return groups;
}

/** A transition verb the detail offers. Block asks for a reason before it fires. */
export type TransitionVerb =
  | "start_todo"
  | "complete_todo"
  | "block_todo"
  | "reopen_todo"
  | "cancel_todo";

/**
 * The most likely next move from a status, drawn as the one primary action: open starts,
 * in progress completes, blocked starts again (which clears the reason), and done or
 * cancelled reopens.
 */
export function primaryTransition(status: string): TransitionVerb {
  switch (status) {
    case "in_progress":
      return "complete_todo";
    case "done":
    case "cancelled":
      return "reopen_todo";
    default:
      return "start_todo"; // open, blocked, and anything else
  }
}

/** Every transition that moves a to-do from `status`: each verb but the one onto it. */
export function transitionsFrom(status: string): TransitionVerb[] {
  const all: [TransitionVerb, string][] = [
    ["start_todo", "in_progress"],
    ["complete_todo", "done"],
    ["block_todo", "blocked"],
    ["reopen_todo", "open"],
    ["cancel_todo", "cancelled"],
  ];
  return all.filter(([, to]) => to !== status).map(([verb]) => verb);
}

/** Human label for a canonical status (board lanes carry their own labels). */
export function statusLabel(status: string): string {
  switch (status) {
    case "in_progress":
      return "In progress";
    case "open":
      return "Open";
    default:
      return status.charAt(0).toUpperCase() + status.slice(1);
  }
}

/**
 * The transition verb a drop onto a target lane maps to. `block` needs a `reason`,
 * surfaced by the caller. Unknown statuses map to null (no transition).
 */
export function transitionForStatus(status: string): TransitionVerb | null {
  switch (status) {
    case "open":
      return "reopen_todo";
    case "in_progress":
      return "start_todo";
    case "blocked":
      return "block_todo";
    case "done":
      return "complete_todo";
    case "cancelled":
      return "cancel_todo";
    default:
      return null;
  }
}
