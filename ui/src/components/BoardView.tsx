import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Inline, Spinner, Stack, Text, tokens } from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { transitionForStatus } from "../format";
import {
  type ApiError,
  type Board,
  type Column,
  isApiError,
  type Lane,
  type ListTodosResult,
  type TodoRow,
} from "../types";
import { StatusMark } from "./marks";
import { TodoCard } from "./TodoCard";
import { BlockTodoForm, NewTodoForm } from "./forms";

/** Per-lane to-do buckets, keyed by canonical status. */
type Buckets = Record<string, TodoRow[]>;

// Cards fetched per lane. The board view is unpaginated, so a lane past this many
// to-dos is truncated — the lane footer surfaces that and points to Table/Search.
const LANE_LIMIT = 200;

/**
 * A lane's cards in their saved position. `list_todos` pages newest first, so the lane sorts
 * by `order` itself, then by creation and id, the order a to-do's steps come in.
 */
function byLanePosition(todos: TodoRow[]): TodoRow[] {
  return [...todos].sort(
    (a, b) =>
      a.order - b.order ||
      a.created_at.localeCompare(b.created_at) ||
      a.id.localeCompare(b.id),
  );
}

/**
 * The kanban board. Lanes come from the board's `columns` (each maps 1:1 to a
 * canonical status); each lane lists `list_todos(board_id, status)`.
 *
 * Drag semantics:
 *  - Drop a card on a DIFFERENT lane → a status transition mapped from the TARGET
 *    lane's status (open→reopen_todo, in_progress→start_todo, blocked→block_todo
 *    [prompts for the reason], done→complete_todo, cancelled→cancel_todo).
 *  - Drop within the SAME lane → reorder_todos(board_id, status, ordered_ids).
 */
export function BoardView({
  board,
  lanes,
  onSelect,
  onMutate,
  reloadKey,
}: {
  board: Board;
  /** get_board_summary lanes — carry counts + wip_limit for the lane header. */
  lanes: Lane[];
  onSelect: (todoId: string) => void;
  onMutate: () => void;
  /** Bump to force a reload (e.g. when the server announces a change). */
  reloadKey: number;
}) {
  const call = useCall();
  const columns: Column[] = (board.columns ?? []).filter((c) => c.status);
  const statusKey = columns.map((c) => c.status).join("\n");
  const [buckets, setBuckets] = useState<Buckets>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [addLane, setAddLane] = useState<string | null>(null);
  // A pending block: a drop onto the blocked lane needs a reason before the verb.
  const [blockTodoId, setBlockTodoId] = useState<string | null>(null);

  // The card currently being dragged: its id + origin lane status.
  const drag = useRef<{ id: string; from: string } | null>(null);
  const [draggingId, setDraggingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next: Buckets = {};
      // One list_todos per lane (statuses are few — four canonical lanes).
      const results = await Promise.all(
        columns.map((col) =>
          call<ListTodosResult | ApiError>("list_todos", {
            board_id: board.id,
            status: col.status,
            limit: LANE_LIMIT,
          }),
        ),
      );
      results.forEach((res, i) => {
        const status = columns[i].status;
        next[status] = isApiError(res) ? [] : byLanePosition(res.todos ?? []);
        if (isApiError(res)) setError(res.message ?? res.error);
      });
      setBuckets(next);
    } catch (err) {
      // A thrown transport/auth rejection — surface it instead of letting it
      // escape as an unhandled rejection with the board silently empty.
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
    // columns is a fresh array each render; the status list is its stable identity,
    // so a re-read that remaps the lanes reloads them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [call, board.id, statusKey]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  const laneCount = (status: string): number => buckets[status]?.length ?? 0;

  const onDropLane = useCallback(
    async (toStatus: string, beforeId: string | null) => {
      const d = drag.current;
      drag.current = null;
      setDraggingId(null);
      if (!d) return;

      if (d.from === toStatus) {
        // In-lane reorder: rebuild the lane's id order with the dragged card moved.
        const lane = buckets[toStatus] ?? [];
        const ids = lane.map((t) => t.id).filter((id) => id !== d.id);
        const insertAt = beforeId ? ids.indexOf(beforeId) : ids.length;
        const at = insertAt < 0 ? ids.length : insertAt;
        const ordered = [...ids.slice(0, at), d.id, ...ids.slice(at)];
        // Optimistic: reflect the new order locally, then persist.
        setBuckets((prev) => ({
          ...prev,
          [toStatus]: ordered.map((id) => lane.find((t) => t.id === id)!).filter(Boolean),
        }));
        try {
          const res = await call<unknown>("reorder_todos", {
            board_id: board.id,
            status: toStatus,
            todo_ids: ordered,
          });
          if (isApiError(res)) setError(res.message ?? res.error);
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
        } finally {
          // Resync regardless: on failure this reload corrects the optimistic order.
          onMutate();
        }
        return;
      }

      // Cross-lane: a status transition mapped from the TARGET lane's status.
      const verb = transitionForStatus(toStatus);
      if (!verb) return;
      if (verb === "block_todo") {
        // block_todo requires a reason — surface the modal; the verb fires on confirm.
        setBlockTodoId(d.id);
        return;
      }
      try {
        const res = await call<unknown>(verb, { todo_id: d.id });
        if (isApiError(res)) setError(res.message ?? res.error);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        // onMutate bumps reloadKey, which re-runs load() via the effect — no
        // explicit load() needed (it would double-fetch, as the reorder branch shows).
        onMutate();
      }
    },
    [buckets, call, board.id, onMutate],
  );

  return (
    <div
      style={{
        height: "100%",
        // border-box so height:100% INCLUDES the padding — content-box would add
        // padding on top of 100% and overflow the pane (clipped, scrollbar adrift).
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        // No bottom padding: the lane row's horizontal scrollbar sits flush at
        // the pane's bottom edge instead of floating above a strip of padding.
        padding: "0.75rem 0.75rem 0",
      }}
    >
      {error ? (
        <Text size="sm" tone="danger" style={{ display: "block", marginBottom: "0.5rem" }}>
          {error}
        </Text>
      ) : null}
      {loading && Object.keys(buckets).length === 0 ? (
        <Inline gap="0.5rem" style={{ padding: "1rem" }}>
          <Spinner />
          <Text size="sm" tone="muted">
            Loading board…
          </Text>
        </Inline>
      ) : (
        <div
          style={{
            display: "flex",
            gap: "0.75rem",
            // Stretch lanes to full height; scroll lanes horizontally (the board
            // itself doesn't scroll — each lane scrolls its cards internally).
            alignItems: "stretch",
            flex: 1,
            minHeight: 0,
            overflowX: "auto",
            overflowY: "hidden",
            // Breathing room below the columns. alignItems:stretch sizes lanes to
            // this row's CONTENT box, so the padding leaves a gap under the lanes —
            // while the horizontal scrollbar still rides the row's border-box bottom,
            // which is flush with the pane edge (the board has no bottom padding).
            paddingBottom: "0.75rem",
          }}
        >
          {columns.map((col) => {
            const laneTodos = buckets[col.status] ?? [];
            const summaryLane = lanes.find((l) => l.status === col.status);
            const wipLimit = summaryLane?.wip_limit;
            const overLimit = wipLimit !== undefined && laneCount(col.status) > wipLimit;
            // Truncated only when the cap was actually hit AND get_board_summary's true
            // count exceeds what we loaded — avoids a false hint on a transient
            // count mismatch between the two reads.
            const laneTotal = summaryLane?.count;
            const truncated =
              laneTodos.length >= LANE_LIMIT &&
              laneTotal !== undefined &&
              laneTotal > laneTodos.length;
            return (
              <div
                key={col.key}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault();
                  void onDropLane(col.status, null);
                }}
                style={{
                  // Lane = raised surface (#1e293b dark / #fafafa light — populated
                  // in both themes), borderless; cards sit on tokens.bg (#0f172a /
                  // #ffffff) with a hairline border so they read as distinct. Never
                  // tokens.bgSubtle (tertiary is unset in the host dark theme →
                  // white-on-white fallback).
                  flex: "0 0 280px",
                  width: 280,
                  boxSizing: "border-box", // padding inside the 280, not added on top
                  // Full-height column: header fixed at top, cards scroll inside.
                  display: "flex",
                  flexDirection: "column",
                  minHeight: 0,
                  background: tokens.bgRaised,
                  borderRadius: 10,
                  padding: 12,
                }}
              >
                <Inline justify="between" align="center" style={{ marginBottom: "0.5rem" }}>
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 8,
                      fontSize: 13,
                      fontWeight: 600,
                      color: tokens.fg,
                      minWidth: 0,
                    }}
                  >
                    <StatusMark status={col.status} />
                    {col.label}
                    <span
                      title={overLimit ? `Over the lane's limit of ${wipLimit}` : undefined}
                      style={{ color: overLimit ? tokens.warning : tokens.fgMuted, fontWeight: 500 }}
                    >
                      {wipLimit !== undefined
                        ? `${laneCount(col.status)} / ${wipLimit}`
                        : laneCount(col.status)}
                    </span>
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Add a to-do to ${col.label}`}
                    onClick={() => setAddLane(col.status)}
                  >
                    +
                  </Button>
                </Inline>
                <Stack gap="8px" style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
                  {laneTodos.map((t) => (
                    <div
                      key={t.id}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        void onDropLane(col.status, t.id);
                      }}
                    >
                      <TodoCard
                        todo={t}
                        dragging={draggingId === t.id}
                        onDragStart={() => {
                          drag.current = { id: t.id, from: col.status };
                          setDraggingId(t.id);
                        }}
                        onClick={() => onSelect(t.id)}
                      />
                    </div>
                  ))}
                  {laneTodos.length === 0 ? (
                    <Text size="xs" tone="faint" style={{ padding: "0.5rem 0.2rem" }}>
                      Empty
                    </Text>
                  ) : null}
                </Stack>
                {truncated ? (
                  // tone="muted" (host-backed), not "faint" — fgFaint is theme-blind.
                  <Text size="xs" tone="muted" style={{ display: "block", padding: "0.5rem 0.2rem 0" }}>
                    Showing first {LANE_LIMIT} of {laneTotal} — use Table or Search for the rest.
                  </Text>
                ) : null}
              </div>
            );
          })}
        </div>
      )}

      {addLane ? (
        <NewTodoForm
          boardId={board.id}
          status={addLane}
          onClose={() => setAddLane(null)}
          onDone={() => {
            void load();
            onMutate();
          }}
        />
      ) : null}
      {blockTodoId ? (
        <BlockTodoForm
          todoId={blockTodoId}
          onClose={() => setBlockTodoId(null)}
          onDone={() => {
            setBlockTodoId(null);
            void load();
            onMutate();
          }}
        />
      ) : null}
    </div>
  );
}
