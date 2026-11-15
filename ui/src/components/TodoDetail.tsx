import { Fragment, useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { ListRow, Spinner, Text, tokens, useBreakpoint } from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import {
  fmtDate,
  isOverdue,
  primaryTransition,
  transitionsFrom,
  type TransitionVerb,
} from "../format";
import { type ApiError, type GetTodoResult, isApiError, type Todo } from "../types";
import { ActivityTimeline, DetailsBody } from "./kit";
import { BlockTodoForm, EditTodoForm } from "./forms";
import { LabelList, PriorityText, StatusText } from "./marks";

type Modal = null | "edit" | "block";

/** The width below which the view drops its properties sidebar for one column. */
export const SPLIT_MIN_WIDTH = 760;

/** An action the view offers: a transition verb, or the edit form, or archive. */
type Action = TransitionVerb | "edit" | "archive_todo";

const ACTION_LABEL: Record<Action, string> = {
  start_todo: "Start",
  complete_todo: "Complete",
  block_todo: "Block",
  reopen_todo: "Reopen",
  cancel_todo: "Cancel",
  edit: "Edit",
  archive_todo: "Archive",
};

/** The actions besides the primary one, in the order they are listed. */
function secondaryActions(t: Todo, primary: TransitionVerb): Action[] {
  const valid = new Set<Action>(transitionsFrom(t.status));
  if (!t.archived_at) valid.add("archive_todo");
  valid.add("edit");
  const order: Action[] = [
    "start_todo",
    "complete_todo",
    "block_todo",
    "edit",
    "archive_todo",
    "reopen_todo",
    "cancel_todo",
  ];
  return order.filter((a) => a !== primary && valid.has(a));
}

const primaryButton: CSSProperties = {
  font: "inherit",
  fontSize: 13,
  fontWeight: 500,
  padding: "6px 12px",
  minHeight: 32,
  borderRadius: 7,
  border: `1px solid ${tokens.fg}`,
  background: tokens.fg,
  color: tokens.bg,
  cursor: "pointer",
};

const secondaryButton: CSSProperties = {
  font: "inherit",
  fontSize: 14,
  padding: "4px 9px",
  minHeight: 32,
  borderRadius: 7,
  border: `1px solid ${tokens.borderStrong}`,
  background: tokens.bg,
  color: tokens.fg,
  cursor: "pointer",
};

const menuItem: CSSProperties = {
  font: "inherit",
  fontSize: 13,
  textAlign: "left",
  padding: "7px 10px",
  minHeight: 32,
  border: 0,
  borderRadius: 6,
  background: "transparent",
  color: tokens.fg,
  cursor: "pointer",
  whiteSpace: "nowrap",
};

const quietButton: CSSProperties = {
  font: "inherit",
  fontSize: 13,
  textAlign: "left",
  padding: "6px 0",
  minHeight: 32,
  border: 0,
  background: "transparent",
  color: tokens.fg,
  cursor: "pointer",
};

const sectionRule: CSSProperties = {
  borderTop: `1px solid ${tokens.border}`,
  paddingTop: 16,
  display: "flex",
  flexDirection: "column",
  gap: 10,
};

const sectionHeading: CSSProperties = {
  margin: 0,
  fontSize: 12,
  fontWeight: 600,
  color: tokens.fgMuted,
};

/**
 * One to-do: get_todo → the to-do, its steps and recent activity. Field edits go through
 * update_todo; status moves through the transition verbs (start/complete/block/reopen/cancel);
 * archive_todo hides it, so a successful archive closes the view. An already-archived to-do
 * (an agent archived it while it was open, or a goal's archived step) stays readable and
 * says so.
 *
 * `layout="auto"` measures the space the view is given: at `SPLIT_MIN_WIDTH` or wider, a main
 * column beside a properties sidebar; narrower, one column. `layout="column"` is always one
 * column (the drawer). Both open on a breadcrumb, `<board> / <KEY>`; `onBack`, when given, makes
 * the board's name a link back to it. One column puts the actions on the breadcrumb's row and
 * one muted line under the title: status, priority when it is not normal, labels, assignee and
 * due date, apart by "·".
 */
export function TodoDetail({
  todoId,
  onMutate,
  onNavigate,
  onClose,
  reloadKey,
  layout = "column",
  boardName,
  onBack,
}: {
  todoId: string;
  onMutate: () => void;
  onClose: () => void;
  onNavigate: (id: string) => void;
  /** Bump to reload in place. Open forms keep what the user typed. */
  reloadKey: number;
  layout?: "auto" | "column";
  /** The name of the board a to-do lives on, when known. */
  boardName?: (boardId: string) => string | undefined;
  /** Go back to the board the to-do lives on. */
  onBack?: (boardId: string | undefined) => void;
}) {
  const call = useCall();
  const [data, setData] = useState<GetTodoResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [modal, setModal] = useState<Modal>(null);
  const [busy, setBusy] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const { ref, isNarrow } = useBreakpoint<HTMLDivElement>(SPLIT_MIN_WIDTH);
  const split = layout === "auto" && !isNarrow;

  const load = useCallback(async () => {
    try {
      const res = await call<GetTodoResult | ApiError>("get_todo", { todo_id: todoId });
      if (isApiError(res)) {
        setError(res.message ?? res.error);
        setData(null);
        return;
      }
      setError(null);
      setData(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setData(null);
    }
  }, [call, todoId]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  // The ··· menu closes on a click outside it or on Escape.
  useEffect(() => {
    if (!moreOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMoreOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMoreOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [moreOpen]);

  // Fire a verb, then refresh the lists and either re-read the detail or, for an
  // archive, close it. A rejected call (transport, auth) shows here like a refused one.
  const transition = useCallback(
    async (tool: string, afterwards: () => void | Promise<void> = load) => {
      setBusy(true);
      try {
        const res = await call<unknown>(tool, { todo_id: todoId });
        if (isApiError(res)) {
          setError(res.message ?? res.error);
          return;
        }
        await afterwards();
        onMutate();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [call, todoId, load, onMutate],
  );

  const run = (action: Action) => {
    setMoreOpen(false);
    if (action === "edit") setModal("edit");
    else if (action === "block_todo") setModal("block");
    else if (action === "archive_todo") void transition("archive_todo", onClose);
    else void transition(action);
  };

  // The breadcrumb: the board, a link back to it whenever there is a way back (a to-do that
  // failed to load still leaves one, to the board already chosen), then the to-do's key.
  const boardId = data?.todo.board_id;
  const board = boardId ? boardName?.(boardId) : undefined;
  const todoKey = data?.todo.key;
  const crumbBoard = onBack ? (
    <button
      type="button"
      onClick={() => onBack(boardId)}
      style={{
        font: "inherit",
        padding: 0,
        border: 0,
        background: "none",
        color: tokens.fgMuted,
        cursor: "pointer",
      }}
    >
      {board ?? "Board"}
    </button>
  ) : board ? (
    <span>{board}</span>
  ) : null;
  const breadcrumb = (
    <nav
      aria-label="Breadcrumb"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        minWidth: 0,
        fontSize: 12.5,
        color: tokens.fgMuted,
      }}
    >
      {crumbBoard}
      {crumbBoard && todoKey ? <span aria-hidden="true">/</span> : null}
      {todoKey ? <span style={{ fontFamily: tokens.fontMono }}>{todoKey}</span> : null}
    </nav>
  );
  // The drawer pads its own body; a view that fills the frame pads itself.
  const pad = layout === "auto" ? "24px 28px" : 0;

  let body: ReactNode;
  if (error || !data) {
    body = (
      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: pad }}>
        {onBack ? breadcrumb : null}
        {error ? (
          <Text size="sm" tone="danger">
            {error}
          </Text>
        ) : (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <Spinner />
            <Text size="sm" tone="muted">
              Loading…
            </Text>
          </span>
        )}
      </div>
    );
  } else {
    const t = data.todo;
    const primary = primaryTransition(t.status);
    const others = secondaryActions(t, primary);
    const title = (
      <h1
        style={{
          margin: 0,
          fontSize: 21,
          fontWeight: 600,
          lineHeight: 1.3,
          color: tokens.fg,
          overflowWrap: "anywhere",
        }}
      >
        {t.title}
      </h1>
    );
    const blocked = t.blocked_reason ? (
      <div style={{ fontSize: 13, color: tokens.warning }}>{t.blocked_reason}</div>
    ) : null;
    const overdue = isOverdue(t.due_at) && t.status !== "done" && t.status !== "cancelled";
    const due = t.due_at ? (
      <span style={{ color: overdue ? tokens.danger : tokens.fgMuted }}>
        {overdue ? "Overdue" : "Due"} {fmtDate(t.due_at)}
      </span>
    ) : null;
    const details = t.details ? (
      <div style={{ fontSize: 14, lineHeight: 1.6, color: tokens.fg }}>
        <DetailsBody markdown={t.details} />
      </div>
    ) : null;
    const steps = data.steps.length ? (
      <section style={sectionRule} aria-label="Steps">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h3 style={sectionHeading}>Steps</h3>
          <span style={{ fontSize: 12, color: tokens.fgMuted }}>
            {data.counts.steps_done} / {data.counts.steps} done
          </span>
        </div>
        <div>
          {data.steps.map((s) => (
            <ListRow
              key={s.id}
              interactive
              onClick={() => onNavigate(s.id)}
              title={s.title}
              meta={s.key}
              trailing={<StatusText status={s.status} style={{ fontSize: 12.5 }} />}
            />
          ))}
        </div>
      </section>
    ) : null;
    const activity = (
      <section style={sectionRule} aria-label="Activity">
        <h3 style={sectionHeading}>Activity</h3>
        <ActivityTimeline activity={data.recent_activity} board={board} createdAt={t.created_at} />
      </section>
    );
    const archived = t.archived_at ? (
      <span style={{ color: tokens.fgMuted }}>Archived</span>
    ) : null;

    if (split) {
      body = (
        <div style={{ display: "flex", flex: 1 }}>
          <div
            style={{
              flex: 1,
              minWidth: 0,
              padding: "24px 26px",
              display: "flex",
              flexDirection: "column",
              gap: 16,
            }}
          >
            {breadcrumb}
            {title}
            {blocked}
            {details}
            {steps}
            {activity}
          </div>
          <aside
            aria-label="Properties"
            style={{
              flex: "0 0 200px",
              boxSizing: "border-box",
              background: tokens.bgRaised,
              borderLeft: `1px solid ${tokens.border}`,
              padding: "24px 18px",
              display: "flex",
              flexDirection: "column",
              gap: 18,
            }}
          >
            <button
              type="button"
              disabled={busy}
              onClick={() => run(primary)}
              style={{ ...primaryButton, padding: "8px 12px" }}
            >
              {ACTION_LABEL[primary]}
            </button>
            <dl
              style={{
                margin: 0,
                display: "flex",
                flexDirection: "column",
                gap: 12,
                fontSize: 13,
                color: tokens.fg,
              }}
            >
              <Property label="Status">
                <StatusText status={t.status} />
                {archived ? <> · {archived}</> : null}
              </Property>
              <Property label="Priority">
                <PriorityText priority={t.priority} />
              </Property>
              {t.labels?.length ? (
                <Property label="Labels">
                  <LabelList labels={t.labels} />
                </Property>
              ) : null}
              {t.assignee ? <Property label="Assignee">{t.assignee}</Property> : null}
              {due ? <Property label="Due">{due}</Property> : null}
              {board ? <Property label="Board">{board}</Property> : null}
              <Property label="Created">{fmtDate(t.created_at)}</Property>
            </dl>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 2,
                borderTop: `1px solid ${tokens.border}`,
                paddingTop: 10,
              }}
            >
              {others.map((a) => (
                <button
                  key={a}
                  type="button"
                  disabled={busy}
                  onClick={() => run(a)}
                  style={quietButton}
                >
                  {ACTION_LABEL[a]}
                </button>
              ))}
            </div>
          </aside>
        </div>
      );
    } else {
      const meta: ReactNode[] = [<StatusText key="status" status={t.status} style={{ color: tokens.fg }} />];
      if (archived) meta.push(<Fragment key="archived">{archived}</Fragment>);
      if (t.priority && t.priority !== "normal") {
        meta.push(<PriorityText key="priority" priority={t.priority} />);
      }
      if (t.labels?.length) meta.push(<LabelList key="labels" labels={t.labels} />);
      if (t.assignee) meta.push(<span key="assignee">{t.assignee}</span>);
      if (due) meta.push(<Fragment key="due">{due}</Fragment>);
      body = (
        <div style={{ display: "flex", flexDirection: "column", gap: 20, padding: pad }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
              }}
            >
              {breadcrumb}
              <div ref={menuRef} style={{ position: "relative", display: "flex", gap: 6, flexShrink: 0 }}>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => run(primary)}
                  style={primaryButton}
                >
                  {ACTION_LABEL[primary]}
                </button>
                <button
                  type="button"
                  aria-label="More actions"
                  aria-expanded={moreOpen}
                  onClick={() => setMoreOpen((v) => !v)}
                  style={secondaryButton}
                >
                  ···
                </button>
                {moreOpen ? (
                  <div
                    style={{
                      position: "absolute",
                      top: "calc(100% + 4px)",
                      right: 0,
                      zIndex: 10,
                      minWidth: 140,
                      padding: 4,
                      display: "flex",
                      flexDirection: "column",
                      background: tokens.bg,
                      border: `1px solid ${tokens.border}`,
                      borderRadius: 8,
                      boxShadow: tokens.shadowMd,
                    }}
                  >
                    {others.map((a) => (
                      <button
                        key={a}
                        type="button"
                        disabled={busy}
                        onClick={() => run(a)}
                        style={menuItem}
                      >
                        {ACTION_LABEL[a]}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
            {title}
            {blocked}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                flexWrap: "wrap",
                gap: "6px 8px",
                fontSize: 13,
                color: tokens.fgMuted,
              }}
            >
              {meta.map((m, i) => (
                <Fragment key={i}>
                  {i > 0 ? <span aria-hidden="true">·</span> : null}
                  {m}
                </Fragment>
              ))}
            </div>
          </div>
          {details}
          {steps}
          {activity}
        </div>
      );
    }
  }

  return (
    <div
      ref={ref}
      style={{
        fontFamily: tokens.fontSans,
        color: tokens.fg,
        minHeight: "100%",
        display: "flex",
        flexDirection: "column",
      }}
    >
      {body}
      {data && modal === "edit" ? (
        <EditTodoForm todo={data.todo} onClose={() => setModal(null)} onDone={onMutate} />
      ) : null}
      {data && modal === "block" ? (
        <BlockTodoForm todoId={data.todo.id} onClose={() => setModal(null)} onDone={onMutate} />
      ) : null}
    </div>
  );
}

/** One property in the sidebar: a small muted name over its value. */
function Property({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt style={{ fontSize: 11.5, color: tokens.fgMuted, marginBottom: 3 }}>{label}</dt>
      <dd style={{ margin: 0, display: "flex", alignItems: "center", flexWrap: "wrap", gap: 6 }}>
        {children}
      </dd>
    </div>
  );
}
