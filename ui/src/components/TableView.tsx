import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Inline,
  Spinner,
  Stack,
  Table,
  Text,
} from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { fmtDate, isOverdue, statusLabel } from "../format";
import { type ApiError, type Board, isApiError, type ListTodosResult, type TodoRow } from "../types";
import { TodoKey } from "./kit";
import { LabelList, PriorityText, StatusText } from "./marks";

type SortKey = "title" | "status" | "priority" | "assignee" | "due_at";

const PRIORITY_RANK: Record<string, number> = { urgent: 0, high: 1, normal: 2, low: 3 };

/**
 * Full-width table view: list_todos with cursor pagination (board-scoped), then a
 * CLIENT-side filter (status / assignee / label) + sort over the loaded pages.
 * Row click opens the detail.
 */
export function TableView({
  board,
  onSelect,
  onMutate: _onMutate,
  reloadKey,
}: {
  board: Board;
  onSelect: (todoId: string) => void;
  onMutate: () => void;
  reloadKey: number;
}) {
  const call = useCall();
  const [todos, setTodos] = useState<TodoRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [statusFilter, setStatusFilter] = useState("");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [labelFilter, setLabelFilter] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("priority");
  const [sortAsc, setSortAsc] = useState(true);

  const loadFirst = useCallback(async () => {
    setLoading(true);
    setError(null);
    setCursor(null);
    try {
      const res = await call<ListTodosResult | ApiError>("list_todos", {
        board_id: board.id,
        limit: 100,
      });
      if (isApiError(res)) {
        setError(res.message ?? res.error);
        setTodos([]);
      } else {
        setTodos(res.todos ?? []);
        setCursor(res.next_cursor ?? null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setTodos([]);
    } finally {
      setLoading(false);
    }
  }, [call, board.id]);

  useEffect(() => {
    void loadFirst();
  }, [loadFirst, reloadKey]);

  const loadMore = useCallback(async () => {
    if (!cursor) return;
    setLoading(true);
    try {
      const res = await call<ListTodosResult | ApiError>("list_todos", {
        board_id: board.id,
        limit: 100,
        cursor,
      });
      if (isApiError(res)) {
        setError(res.message ?? res.error);
      } else {
        setTodos((prev) => [...prev, ...(res.todos ?? [])]);
        setCursor(res.next_cursor ?? null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [call, board.id, cursor]);

  // Distinct assignees / labels across the loaded pages, for the filter dropdowns.
  const assignees = useMemo(
    () => Array.from(new Set(todos.map((t) => t.assignee).filter(Boolean))) as string[],
    [todos],
  );
  const labels = useMemo(
    () => Array.from(new Set(todos.flatMap((t) => t.labels ?? []))),
    [todos],
  );

  const visible = useMemo(() => {
    let rows = todos;
    if (statusFilter) rows = rows.filter((t) => t.status === statusFilter);
    if (assigneeFilter) rows = rows.filter((t) => t.assignee === assigneeFilter);
    if (labelFilter) rows = rows.filter((t) => (t.labels ?? []).includes(labelFilter));
    const sorted = [...rows].sort((a, b) => {
      let cmp = 0;
      if (sortKey === "priority") {
        cmp = (PRIORITY_RANK[a.priority] ?? 9) - (PRIORITY_RANK[b.priority] ?? 9);
      } else if (sortKey === "due_at") {
        cmp = (a.due_at ?? "~").localeCompare(b.due_at ?? "~");
      } else {
        cmp = String(a[sortKey] ?? "").localeCompare(String(b[sortKey] ?? ""));
      }
      return sortAsc ? cmp : -cmp;
    });
    return sorted;
  }, [todos, statusFilter, assigneeFilter, labelFilter, sortKey, sortAsc]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc((v) => !v);
    else {
      setSortKey(key);
      setSortAsc(true);
    }
  };

  const sortHeader = (label: string, key: SortKey) => (
    <button
      onClick={() => toggleSort(key)}
      style={{ background: "none", border: "none", padding: 0, cursor: "pointer", font: "inherit", color: "inherit" }}
    >
      {label}
      {sortKey === key ? (sortAsc ? " ↑" : " ↓") : ""}
    </button>
  );

  return (
    <div
      style={{
        height: "100%",
        // border-box so height:100% includes the padding (else content-box makes
        // the box 100% + 1.5rem, overflowing the pane and clipping its bottom).
        boxSizing: "border-box",
        overflow: "auto",
        // All-sides padding (the board drops its bottom): this container owns the
        // scroll (overflow:auto), so its scrollbar rides the border-box edge no
        // matter the padding — the bottom padding is just breathing room below the
        // last row, not a strip pushing a scrollbar off the edge.
        padding: "0.75rem",
      }}
    >
      <Stack gap="0.75rem">
        <Inline gap="0.5rem" wrap align="center">
          <Filter
            label="Status"
            value={statusFilter}
            onChange={setStatusFilter}
            options={["open", "in_progress", "blocked", "done", "cancelled"].map((s) => ({
              value: s,
              label: statusLabel(s),
            }))}
          />
          <Filter
            label="Assignee"
            value={assigneeFilter}
            onChange={setAssigneeFilter}
            options={assignees.map((a) => ({ value: a, label: a }))}
          />
          <Filter
            label="Label"
            value={labelFilter}
            onChange={setLabelFilter}
            options={labels.map((l) => ({ value: l, label: l }))}
          />
          <Text size="xs" tone="faint">
            {visible.length} of {todos.length}
          </Text>
        </Inline>

        {error ? (
          <Text size="sm" tone="danger">
            {error}
          </Text>
        ) : null}

        <Table<TodoRow>
          data={visible}
          rowKey={(t) => t.id}
          onRowClick={(t) => onSelect(t.id)}
          empty={
            loading ? (
              <Inline gap="0.5rem">
                <Spinner />
                <Text size="sm" tone="muted">
                  Loading…
                </Text>
              </Inline>
            ) : (
              <Text size="sm" tone="faint">
                No todos match.
              </Text>
            )
          }
          columns={[
            { key: "key", header: "Key", render: (t) => <TodoKey value={t.key} size="sm" /> },
            { key: "title", header: sortHeader("Title", "title"), render: (t) => t.title },
            {
              key: "status",
              header: sortHeader("Status", "status"),
              render: (t) => <StatusText status={t.status} />,
            },
            {
              key: "priority",
              header: sortHeader("Priority", "priority"),
              render: (t) => <PriorityText priority={t.priority} />,
            },
            {
              key: "assignee",
              header: sortHeader("Assignee", "assignee"),
              render: (t) => t.assignee ?? "—",
            },
            {
              key: "due_at",
              header: sortHeader("Due", "due_at"),
              render: (t) =>
                t.due_at ? (
                  <Text
                    size="sm"
                    tone={
                      isOverdue(t.due_at) && t.status !== "done" && t.status !== "cancelled"
                        ? "danger"
                        : "default"
                    }
                  >
                    {fmtDate(t.due_at)}
                  </Text>
                ) : (
                  "—"
                ),
            },
            {
              key: "labels",
              header: "Labels",
              render: (t) =>
                t.labels && t.labels.length ? (
                  <LabelList labels={t.labels} />
                ) : (
                  "—"
                ),
            },
          ]}
        />

        {cursor ? (
          <Button variant="ghost" size="sm" onClick={() => void loadMore()} disabled={loading}>
            Load more
          </Button>
        ) : null}
      </Stack>
    </div>
  );
}

function Filter({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      style={{ padding: "0.3rem 0.5rem", borderRadius: 8 }}
    >
      <option value="">{label}: all</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
