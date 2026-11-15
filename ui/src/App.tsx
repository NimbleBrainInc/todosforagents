import { useCallback, useEffect, useRef, useState } from "react";
import { hostSupports } from "@nimblebrain/synapse";
import {
  useApp,
  useDataSync,
  useModelContext,
  useToolInput,
  useToolResult,
} from "@nimblebrain/synapse/react";
import {
  AppFrame,
  Button,
  Drawer,
  EmptyState,
  Heading,
  Inline,
  ListRow,
  SearchField,
  SegmentedControl,
  Spinner,
  Stack,
  Text,
  tokens,
  useBreakpoint,
} from "@nimblebrain/synapse/ui";
import { useCall } from "./api";
import { detailTarget } from "./launch";
import { BoardView } from "./components/BoardView";
import { TableView } from "./components/TableView";
import { TodoDetail } from "./components/TodoDetail";
import { PriorityIcon, StatusText } from "./components/marks";
import { NewBoardForm } from "./components/forms";
import {
  type ApiError,
  type Board,
  type BoardSummaryResult,
  isApiError,
  type Lane,
  type ListBoardsResult,
  type ListTodosResult,
  type TodoRow,
} from "./types";

type ViewMode = "board" | "table";

const VIEW_OPTIONS: { label: string; value: ViewMode }[] = [
  { label: "Board", value: "board" },
  { label: "Table", value: "table" },
];

export default function App() {
  const app = useApp();
  const call = useCall();

  const [view, setView] = useState<ViewMode>("board");
  // `null` until the first read answers: the only time there is nothing to show. A re-read
  // (every write announces one) keeps the current boards, and the views under them mounted,
  // until the new answer replaces them.
  const [boards, setBoards] = useState<Board[] | null>(null);
  const [boardId, setBoardId] = useState<string | null>(null);
  const [lanes, setLanes] = useState<Lane[]>([]);

  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<TodoRow[]>([]);

  // The drawer over the board, opened by a click on a card, a row or a search hit.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // The to-do a get_todo call opened the view on, shown alone with no board behind it.
  const [focusId, setFocusId] = useState<string | null>(null);
  const [newBoardOpen, setNewBoardOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped to force the active view + open detail to reload after a mutation.
  const [reloadKey, setReloadKey] = useState(0);

  // Opened by a `get_todo` call, the view shows that to-do alone, with a link to its board;
  // opened by `get_board_summary` or by no tool, the board. It focuses once per call: leaving
  // for the board stays there. The board is chosen once too, when the result names it,
  // which can be after the input already opened the to-do, so the link leads to its board.
  const toolInput = useToolInput();
  const toolResult = useToolResult();
  const openedFor = useRef<string | null>(null);
  const boardFor = useRef<string | null>(null);
  const toolName = app.toolInfo?.tool?.name;
  useEffect(() => {
    const target = detailTarget(
      typeof toolName === "string" ? toolName : undefined,
      toolInput,
      toolResult?.structuredContent,
    );
    if (!target) return;
    if (openedFor.current !== target.todoId) {
      openedFor.current = target.todoId;
      setFocusId(target.todoId);
    }
    if (target.boardId && boardFor.current !== target.todoId) {
      boardFor.current = target.todoId;
      setBoardId(target.boardId);
    }
  }, [toolName, toolInput, toolResult]);

  // Opened by `get_board_summary`, the view selects the board it summarized: the result's
  // board, or the board the input names by id or key until the result arrives. It selects
  // once per board named, so a later pick from the selector stays.
  const summaryFor = useRef<string | null>(null);
  useEffect(() => {
    if (toolName !== "get_board_summary" || !boards) return;
    const summarized = (toolResult?.structuredContent as { board?: { id?: unknown } } | undefined)?.board?.id;
    const named = typeof summarized === "string" ? summarized : toolInput?.board_id;
    const ref = typeof named === "string" ? named.trim() : "";
    if (!ref || summaryFor.current === ref) return;
    const match = boards.find((b) => b.id === ref || b.key.toLowerCase() === ref.toLowerCase());
    if (!match) return;
    summaryFor.current = ref;
    setBoardId(match.id);
  }, [toolName, toolInput, toolResult, boards]);

  const searching = query.trim().length > 0;
  const board = boards?.find((b) => b.id === boardId) ?? null;

  // The board/table fills the whole body; the detail rides in a side Drawer. On a
  // narrow host pane the drawer spans (almost) the full width so the detail stays
  // legible; on a wide pane it's a fixed panel that leaves the board visible.
  const { ref: bodyRef, isNarrow } = useBreakpoint<HTMLDivElement>(720);
  const drawerWidth = isNarrow ? "100%" : 480;

  // Debounce the search box so each keystroke doesn't fire a tool call.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(queryInput), 250);
    return () => clearTimeout(timer);
  }, [queryInput]);

  // Load the workspace's boards; default to the is_default board (or the first).
  const loadBoards = useCallback(async () => {
    try {
      const res = await call<ListBoardsResult | ApiError>("list_boards");
      if (isApiError(res)) {
        setError(res.message ?? res.error);
        setBoards([]);
      } else {
        setError(null);
        const list = res.boards ?? [];
        setBoards(list);
        setBoardId((prev) => {
          if (prev && list.some((b) => b.id === prev)) return prev;
          return (list.find((b) => b.is_default) ?? list[0])?.id ?? null;
        });
      }
    } catch (err) {
      // A genuine transport/auth rejection (token expiry, host disconnect) —
      // surface it and clear the spinner so the app never sticks on "Loading…".
      setError(err instanceof Error ? err.message : String(err));
      setBoards([]);
    }
  }, [call]);

  useEffect(() => {
    void loadBoards();
  }, [loadBoards]);

  // get_board_summary gives the lane labels/counts/wip for the selected board.
  const loadSummary = useCallback(async () => {
    if (!boardId) {
      setLanes([]);
      return;
    }
    try {
      const res = await call<BoardSummaryResult | ApiError>("get_board_summary", { board_id: boardId });
      setLanes(isApiError(res) ? [] : (res.lanes ?? []));
    } catch {
      // Lane counts/WIP are decorative — degrade to no badges rather than
      // throwing unhandled; BoardView still renders from board.columns.
      setLanes([]);
    }
  }, [call, boardId]);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary, reloadKey]);

  // Run the search when the debounced query changes.
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setHits([]);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const res = await call<ListTodosResult | ApiError>("list_todos", {
          query: q,
          board_id: boardId ?? undefined,
          limit: 50,
        });
        if (cancelled) return;
        setHits(isApiError(res) ? [] : (res.todos ?? []));
      } catch {
        if (!cancelled) setHits([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [call, query, boardId, reloadKey]);

  // Refresh everything, the open detail included, when to-dos change.
  const onMutate = useCallback(() => {
    setReloadKey((k) => k + 1);
  }, []);

  useDataSync(() => {
    void loadBoards();
    onMutate();
  });

  useModelContext(
    () => ({
      state: {
        view: focusId ? "todo" : view,
        boardId,
        boardName: board?.name ?? null,
        query: query.trim(),
        selectedId: focusId ?? selectedId,
      },
      summary: (focusId ?? selectedId)
        ? `Viewing todo ${focusId ?? selectedId}`
        : board
          ? `Viewing the "${board.name}" board (${view})`
          : "Browsing todos",
    }),
    [view, boardId, board, query, selectedId, focusId],
  );

  // A host that shows the page title in its own bar (`ai.nimblebrain/location`) already says
  // "To-dos"; the header then leaves its own out rather than say it twice.
  const hostShowsTitle = hostSupports(app, "location");

  const boardName = useCallback(
    (id: string) => boards?.find((b) => b.id === id)?.name,
    [boards],
  );

  if (focusId) {
    return (
      <AppFrame contentWidth="full">
        <AppFrame.Body bleed>
          <div style={{ height: "100%", overflowY: "auto" }}>
            <TodoDetail
              key={focusId}
              todoId={focusId}
              layout="auto"
              boardName={boardName}
              onBack={(id) => {
                if (id && boards?.some((b) => b.id === id)) setBoardId(id);
                setFocusId(null);
              }}
              onMutate={onMutate}
              onNavigate={setFocusId}
              onClose={() => setFocusId(null)}
              reloadKey={reloadKey}
            />
          </div>
        </AppFrame.Body>
      </AppFrame>
    );
  }

  return (
    <AppFrame contentWidth="full">
      <AppFrame.Header style={{ borderBottom: `1px solid ${tokens.border}` }}>
        <Stack gap="0.85rem">
          <Inline justify="between" align="center" wrap gap="0.5rem">
            {hostShowsTitle ? null : <Heading size="md">To-dos</Heading>}
            <Inline gap="0.5rem">
              <Button variant="ghost" onClick={() => setNewBoardOpen(true)}>
                + Board
              </Button>
              <SegmentedControl options={VIEW_OPTIONS} value={view} onChange={setView} />
            </Inline>
          </Inline>
          <Inline gap="0.75rem" wrap align="center">
            {boards?.length ? (
              <select
                value={boardId ?? ""}
                onChange={(e) => {
                  setBoardId(e.target.value);
                  setSelectedId(null);
                }}
                aria-label="Board"
                style={{ padding: "0.35rem 0.6rem", borderRadius: 8 }}
              >
                {boards.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name} ({b.key}){b.is_default ? " · default" : ""}
                  </option>
                ))}
              </select>
            ) : null}
            <div style={{ flex: 1, minWidth: 200 }}>
              <SearchField
                variant="boxed"
                placeholder="Search to-dos…"
                value={queryInput}
                onChange={(e) => setQueryInput(e.target.value)}
                style={{ width: "100%" }}
              />
            </div>
          </Inline>
        </Stack>
      </AppFrame.Header>

      <AppFrame.Body bleed>
        {/* Full-width main: the board/table fills the whole body. The detail rides
            in the Drawer below, so nothing is selected → the board uses every pixel. */}
        <div ref={bodyRef} style={{ height: "100%", minWidth: 0, overflow: "hidden" }}>
          {error ? (
            <div style={{ padding: "1rem" }}>
              <Text size="sm" tone="danger">
                {error}
              </Text>
            </div>
          ) : boards === null ? (
            <Inline gap="0.5rem" style={{ padding: "1rem" }}>
              <Spinner />
              <Text size="sm" tone="muted">
                Loading…
              </Text>
            </Inline>
          ) : searching ? (
            <div style={{ height: "100%", overflowY: "auto" }}>
              <SearchResults hits={hits} query={query} onSelect={setSelectedId} />
            </div>
          ) : !board ? (
            <EmptyState
              title="No boards yet"
              description="Create a board, or ask the agent to plan some to-dos."
              action={<Button onClick={() => setNewBoardOpen(true)}>+ Board</Button>}
            />
          ) : view === "board" ? (
            <BoardView
              board={board}
              lanes={lanes}
              onSelect={setSelectedId}
              onMutate={onMutate}
              reloadKey={reloadKey}
            />
          ) : (
            <TableView
              board={board}
              onSelect={setSelectedId}
              onMutate={onMutate}
              reloadKey={reloadKey}
            />
          )}
        </div>
      </AppFrame.Body>

      {/* Todo detail: slides in from the right when a todo is selected. The kit Drawer
          (sandbox-safe <div> overlay since @nimblebrain/synapse 0.12.1) renders nothing
          when closed, so mounting only on selection keeps the board full-width — belt-
          and-suspenders now, not a workaround. */}
      {selectedId ? (
        <Drawer open onClose={() => setSelectedId(null)} side="right" width={drawerWidth}>
          <Drawer.Header title="To-do" onClose={() => setSelectedId(null)} />
          <Drawer.Body style={{ padding: "1.25rem" }}>
            <TodoDetail
              key={selectedId}
              todoId={selectedId}
              boardName={boardName}
              onBack={(id) => {
                if (id && boards?.some((b) => b.id === id)) setBoardId(id);
                setSelectedId(null);
              }}
              onMutate={onMutate}
              onNavigate={setSelectedId}
              onClose={() => setSelectedId(null)}
              reloadKey={reloadKey}
            />
          </Drawer.Body>
        </Drawer>
      ) : null}

      {newBoardOpen ? (
        <NewBoardForm
          onClose={() => setNewBoardOpen(false)}
          onDone={() => {
            void loadBoards();
            onMutate();
          }}
        />
      ) : null}
    </AppFrame>
  );
}

/** Search results — a `ListRow` master list (never a Table in the rail). */
function SearchResults({
  hits,
  query,
  onSelect,
}: {
  hits: TodoRow[];
  query: string;
  onSelect: (id: string) => void;
}) {
  if (hits.length === 0) {
    return <EmptyState title="No matches" description={`Nothing matched "${query}".`} />;
  }
  return (
    <div style={{ padding: "0.5rem" }}>
      <Stack gap="0.1rem">
        {hits.map((t) => (
          <ListRow
            key={t.id}
            interactive
            onClick={() => onSelect(t.id)}
            title={t.title}
            meta={t.assignee ? `${t.key} · ${t.assignee}` : t.key}
            trailing={
              <span
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 12,
                  fontSize: 12.5,
                  color: tokens.fg,
                  whiteSpace: "nowrap",
                }}
              >
                <PriorityIcon priority={t.priority} />
                <StatusText status={t.status} />
              </span>
            }
          />
        ))}
      </Stack>
    </div>
  );
}
