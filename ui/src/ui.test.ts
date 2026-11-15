import { ToolCallError } from "@nimblebrain/synapse";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

// The host bridge is the boundary, not the subject: `app.callTool` and the handshake's
// `hostCapabilities` stand in for the host, so the real `App` runs against them. ONE app
// object across renders, because `useCall` memoizes on it. `vi.hoisted` because `vi.mock` is
// hoisted above the imports.
// `dataSync.fire` stands in for the host relaying the server's `list_changed`. `launch` is the
// tool call the host rendered the view for (`toolInfo`, the input and the result it delivers).
const { callToolSpy, appStub, dataSync, launch } = vi.hoisted(() => {
  const callToolSpy = vi.fn();
  return {
    callToolSpy,
    appStub: {
      callTool: callToolSpy,
      hostCapabilities: { experimental: {} as Record<string, object> },
      toolInfo: null as { tool: Record<string, unknown> } | null,
    },
    dataSync: { fire: (() => {}) as () => void },
    launch: {
      input: null as Record<string, unknown> | null,
      result: null as { structuredContent: unknown } | null,
    },
  };
});
vi.mock("@nimblebrain/synapse/react", () => ({
  useApp: () => appStub,
  useDataSync: (callback: () => void) => {
    dataSync.fire = callback;
  },
  useModelContext: () => {},
  useToolInput: () => launch.input,
  useToolResult: () => launch.result,
}));

import App from "./App";
import { ActivityTimeline } from "./components/kit";
import { CardMeta, PriorityIcon } from "./components/marks";
import { NewTodoForm } from "./components/forms";
import { TodoCard } from "./components/TodoCard";
import { activityEntries, editedSentence, joinNatural } from "./format";
import { toApiError } from "./types";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

/** Mount the real `App` against a host that declared `experimental`, with no boards yet. */
async function mountApp(experimental: Record<string, object>): Promise<void> {
  appStub.hostCapabilities = { experimental };
  callToolSpy.mockReset();
  callToolSpy.mockImplementation(async (name: string) =>
    name === "list_boards" ? { data: { boards: [] } } : { data: {} },
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(createElement(App)));
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
}

const heading = () =>
  [...container.querySelectorAll("h1, h2, h3")].find((h) => h.textContent === "To-dos");
const button = (label: string) =>
  [...container.querySelectorAll("button")].find((b) => b.textContent?.includes(label));

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  appStub.toolInfo = null;
  launch.input = null;
  launch.result = null;
});

// A host that declares `ai.nimblebrain/location` draws the page title in its own bar, so the
// header leaves its own out; any other host gets it. The capability the handshake declared is
// the only thing that decides it.
describe("the header's title, against the host's declaration", () => {
  it("is left out where the host shows it, and the header's controls stay", async () => {
    await mountApp({ "ai.nimblebrain/location": {} });
    expect(heading()).toBeUndefined();
    expect(button("+ Board")).toBeDefined();
  });

  it("is drawn on a host that does not declare it", async () => {
    await mountApp({});
    expect(heading()).toBeDefined();
    expect(button("+ Board")).toBeDefined();
  });
});

const BOARD = {
  id: "b1",
  key: "LAU",
  name: "Launch",
  is_default: true,
  columns: [{ key: "todo", label: "To do", status: "todo" }],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};
const row = (id: string, title: string, number = 1) => ({
  id,
  key: `LAU-${number}`,
  board_id: "b1",
  title,
  status: "todo",
  priority: "normal",
  order: 0,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

const flush = () =>
  act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });

/** Mount `App` over one board; `list_boards` answers through `boardsReply`, `list_todos` with `todos`. */
async function mountBoard(state: { todos: ReturnType<typeof row>[]; boardsReply: () => Promise<unknown> }) {
  appStub.hostCapabilities = { experimental: {} };
  callToolSpy.mockReset();
  callToolSpy.mockImplementation(async (name: string) => {
    if (name === "list_boards") return { data: await state.boardsReply() };
    if (name === "list_todos") return { data: { todos: state.todos, next_cursor: null } };
    return { data: {} };
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(createElement(App)));
  await flush();
}

// Every write (the person's or the agent's) announces `list_changed`, and the view answers
// with a full re-read. The spinner stands in only while there is nothing to show yet; a
// re-read keeps the rows on screen, and the view under them mounted, until the answer lands.
describe("a re-read after a write", () => {
  it("shows the spinner on the first load, before the boards answer", async () => {
    await mountBoard({ todos: [], boardsReply: () => new Promise(() => {}) });
    expect(container.textContent).toContain("Loading…");
  });

  it.each(["Board", "Table"])("keeps the %s view's rows on screen until the new ones arrive", async (view) => {
    const state = {
      todos: [row("t1", "Write the spec")],
      boardsReply: async (): Promise<unknown> => ({ boards: [BOARD] }),
    };
    await mountBoard(state);
    const toggle = [...container.querySelectorAll("button")].find((b) => b.textContent === view);
    await act(async () => toggle?.click());
    await flush();
    expect(container.textContent).toContain("Write the spec");

    // The agent adds a to-do. The re-read's boards stay pending: the rows already on
    // screen must hold it meanwhile, not give way to a spinner.
    state.todos = [row("t1", "Write the spec"), row("t2", "Ship it")];
    let answerBoards: (v: unknown) => void = () => {};
    state.boardsReply = () => new Promise((resolve) => (answerBoards = resolve));
    await act(async () => dataSync.fire());
    expect(container.textContent).not.toContain("Loading");
    expect(container.textContent).toContain("Write the spec");

    answerBoards({ boards: [BOARD] });
    await flush();
    expect(container.textContent).not.toContain("Loading");
    expect(container.textContent).toContain("Ship it");
  });

  it("loads a lane the re-read adds to the board", async () => {
    const state = {
      todos: [row("t1", "Write the spec"), { ...row("t2", "Ship it"), status: "in_progress" }],
      boardsReply: async (): Promise<unknown> => ({ boards: [BOARD] }),
    };
    await mountBoard(state);
    // list_todos answers per lane, by the status it is asked for.
    const respond = callToolSpy.getMockImplementation()!;
    callToolSpy.mockImplementation(async (name: string, args?: { status?: string }) =>
      name === "list_todos"
        ? { data: { todos: state.todos.filter((t) => t.status === args?.status), next_cursor: null } }
        : respond(name, args),
    );
    await act(async () => dataSync.fire());
    await flush();
    expect(container.textContent).not.toContain("Ship it");

    // update_board adds an in-progress lane to the same board.
    const columns = [...BOARD.columns, { key: "doing", label: "Doing", status: "in_progress" }];
    state.boardsReply = async () => ({ boards: [{ ...BOARD, columns }] });
    await act(async () => dataSync.fire());
    await flush();
    expect(container.textContent).toContain("Ship it");
  });
});

// list_todos pages newest first; a lane shows its cards in the position reorder_todos saved.
describe("a lane's order", () => {
  it("follows the saved positions, not the listing's", async () => {
    const first = { ...row("t1", "First", 1), order: 0 };
    const second = { ...row("t2", "Second", 2), order: 1, created_at: "2026-01-02T00:00:00Z" };
    await mountBoard({ todos: [second, first], boardsReply: async () => ({ boards: [BOARD] }) });
    const text = container.textContent ?? "";
    expect(text.indexOf("First")).toBeGreaterThan(-1);
    expect(text.indexOf("First")).toBeLessThan(text.indexOf("Second"));
  });
});

// Archive hides a to-do from every default view, so nothing is left for its detail to show.
describe("archiving from the detail drawer", () => {
  const TODO = { ...row("t1", "Write the spec"), details: "" };

  /** Mount one board, then open `t1`'s drawer; `get_todo` and `archive_todo` answer as given. */
  async function openDrawer(replies: { todo?: object; archive?: () => Promise<unknown> } = {}) {
    await mountBoard({
      todos: [row("t1", "Write the spec")],
      boardsReply: async () => ({ boards: [BOARD] }),
    });
    const respond = callToolSpy.getMockImplementation()!;
    callToolSpy.mockImplementation(async (name: string, args?: object) => {
      if (name === "get_todo") {
        return {
          data: {
            todo: replies.todo ?? TODO,
            steps: [],
            counts: { steps: 0, steps_done: 0 },
            recent_activity: [],
          },
        };
      }
      if (name === "archive_todo") return { data: await (replies.archive ?? (async () => ({})))() };
      return respond(name, args);
    });
    const card = [...container.querySelectorAll<HTMLElement>("*")]
      .filter((el) => el.textContent === "Write the spec")
      .at(-1);
    await act(async () => card?.click());
    await flush();
    expect(document.body.textContent).toContain("Activity");
  }

  // The drawer is one column: archive is among the actions behind "···".
  const clickArchive = async () => {
    const more = document.querySelector<HTMLButtonElement>("button[aria-label='More actions']");
    await act(async () => more?.click());
    const archive = [...document.querySelectorAll("button")].find((b) => b.textContent === "Archive");
    await act(async () => archive?.click());
    await flush();
  };

  it("closes the drawer once the archive succeeds", async () => {
    await openDrawer();
    await clickArchive();
    expect(callToolSpy.mock.calls.some(([name]) => name === "archive_todo")).toBe(true);
    expect(document.body.textContent).not.toContain("Activity");
  });

  it.each([
    ["refuses", async () => ({ error: "conflict", message: "It went wrong" })],
    [
      "fails",
      async () => {
        throw new Error("It went wrong");
      },
    ],
  ])("keeps the drawer open with the reason when the call %s", async (_, archive) => {
    await openDrawer({ archive });
    await clickArchive();
    expect(document.body.textContent).toContain("It went wrong");
    expect(document.body.textContent).toContain("To-do");
  });

  // A failure is a result flagged isError, in the server's error shape. A bridge resolves with
  // the flag or rejects with a ToolCallError; either way the view shows the server's message.
  const ERROR_BODY = {
    error: { code: "invalid_request", message: "It went wrong", retryable: false },
    next_step: "Correct the argument and retry.",
  };
  it.each([
    ["resolves with the error flag", async () => ({ isError: true, data: ERROR_BODY })],
    [
      "rejects with a ToolCallError",
      async () => {
        throw new ToolCallError("archive_todo", {
          isError: true,
          structuredContent: ERROR_BODY,
          content: [{ type: "text", text: JSON.stringify(ERROR_BODY) }],
        });
      },
    ],
  ])("keeps the drawer open with the server's message when the bridge %s", async (_, reply) => {
    await openDrawer();
    const respond = callToolSpy.getMockImplementation()!;
    callToolSpy.mockImplementation(async (name: string, args?: object) =>
      name === "archive_todo" ? reply() : respond(name, args),
    );
    await clickArchive();
    expect(document.body.textContent).toContain("It went wrong");
    expect(document.body.textContent).toContain("To-do");
  });

  it("marks a to-do that is already archived and offers no second archive", async () => {
    await openDrawer({ todo: { ...TODO, archived_at: "2026-01-02T00:00:00Z" } });
    const more = document.querySelector<HTMLButtonElement>("button[aria-label='More actions']");
    await act(async () => more?.click());
    const labels = [...document.querySelectorAll("button")].map((b) => b.textContent);
    expect(labels).not.toContain("Archive");
    expect(document.body.textContent).toContain("Archived");
  });
});

// A to-do added to the Blocked lane starts blocked, and the server refuses one without a reason,
// so the form asks for it: no submit until the reason has text, which is sent trimmed. Another
// lane sends no reason.
describe("the new to-do form, by lane", () => {
  async function mountForm(status?: string) {
    callToolSpy.mockReset();
    callToolSpy.mockImplementation(async () => ({ data: { todo: {} } }));
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () =>
      root.render(createElement(NewTodoForm, { boardId: "b1", status, onClose: () => {}, onDone: () => {} })),
    );
  }

  const fill = async (label: string, value: string) => {
    const field = [...container.querySelectorAll("label")]
      .find((l) => l.textContent?.startsWith(label))
      ?.querySelector<HTMLInputElement | HTMLTextAreaElement>("input, textarea");
    expect(field).toBeTruthy();
    const proto = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    await act(async () => {
      Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(field, value);
      field!.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const add = () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Add todo")!;
  const creates = () => callToolSpy.mock.calls.filter(([name]) => name === "create_todo");

  it.each([
    ["empty", ""],
    ["whitespace", "   "],
  ])("holds a blocked to-do with an %s reason", async (_, reason) => {
    await mountForm("blocked");
    await fill("Title", "Ship it");
    await fill("Why is it blocked?", reason);
    expect(add().disabled).toBe(true);
    await act(async () => add().click());
    await flush();
    expect(creates()).toHaveLength(0);
  });

  it("sends a blocked to-do's reason trimmed", async () => {
    await mountForm("blocked");
    await fill("Title", "Ship it");
    await fill("Why is it blocked?", "  waiting on review  ");
    await act(async () => add().click());
    await flush();
    expect(creates()).toHaveLength(1);
    expect(creates()[0][1]).toMatchObject({ board_id: "b1", status: "blocked", blocked_reason: "waiting on review" });
  });

  it("asks no reason on another lane and sends none", async () => {
    await mountForm("open");
    expect(container.textContent).not.toContain("Why is it blocked?");
    await fill("Title", "Ship it");
    await act(async () => add().click());
    await flush();
    expect(creates()).toHaveLength(1);
    expect(creates()[0][1]).toMatchObject({ status: "open" });
    expect(creates()[0][1]).not.toHaveProperty("blocked_reason");
  });
});

// An MCP Apps host renders this view for the tool bound to it. Opened by `get_todo`, it shows
// that to-do alone with a link to its board; opened by `get_board_summary`, the board alone. A
// click on a card opens the to-do in a drawer over the board instead.
describe("the view a tool call opens", () => {
  const OTHER = { ...BOARD, id: "b2", key: "MAG", name: "Magnificent", is_default: false };
  const SHIP = { ...row("t2", "Ship it", 3), key: "MAG-3", board_id: "b2", details: "" };
  const GET_TODO = {
    todo: SHIP,
    steps: [],
    counts: { steps: 0, steps_done: 0 },
    recent_activity: [],
  };

  async function mountFor(tool: string | null, input: object | null, result: object | null) {
    appStub.toolInfo = tool ? { tool: { name: tool } } : null;
    launch.input = input as Record<string, unknown> | null;
    launch.result = result ? { structuredContent: result } : null;
    appStub.hostCapabilities = { experimental: {} };
    callToolSpy.mockReset();
    callToolSpy.mockImplementation(async (name: string) => {
      if (name === "list_boards") return { data: { boards: [BOARD, OTHER] } };
      if (name === "list_todos") return { data: { todos: [row("t1", "Write the spec")], next_cursor: null } };
      if (name === "get_todo") return { data: GET_TODO };
      return { data: {} };
    });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root.render(createElement(App)));
    await flush();
  }

  const drawerOpen = () => document.querySelector("[role=dialog]") !== null;
  const focused = () => !drawerOpen() && (document.body.textContent?.includes("Activity") ?? false);
  const board = () => container.querySelector<HTMLSelectElement>("select[aria-label=Board]")?.value;
  const crumb = () => container.querySelector("nav[aria-label=Breadcrumb]");
  const backLink = () => crumb()?.querySelector("button");

  it("shows the to-do alone, under a breadcrumb whose board links back to it", async () => {
    await mountFor("get_todo", { todo_id: "mag-3" }, GET_TODO);
    expect(focused()).toBe(true);
    expect(crumb()?.textContent).toBe("Magnificent/MAG-3");
    expect(container.textContent?.match(/MAG-3/g)).toHaveLength(1);
    expect(container.textContent).toContain("Ship it");
    expect(container.textContent).not.toContain("Write the spec");
    expect(board()).toBeUndefined();
    expect(backLink()?.textContent).toBe("Magnificent");

    await act(async () => backLink()?.click());
    await flush();
    expect(focused()).toBe(false);
    expect(board()).toBe("b2");
    expect(container.textContent).toContain("Write the spec");
  });

  it("reads the to-do the input names while the result is still on its way", async () => {
    await mountFor("get_todo", { todo_id: "MAG-3" }, null);
    expect(focused()).toBe(true);
    expect(callToolSpy).toHaveBeenCalledWith("get_todo", { todo_id: "MAG-3" });
  });

  it("moves to the result's board when it arrives after the input opened the to-do", async () => {
    await mountFor("get_todo", { todo_id: "t2" }, null);
    expect(focused()).toBe(true);
    launch.result = { structuredContent: GET_TODO };
    await act(async () => root.render(createElement(App)));
    await flush();
    await act(async () => backLink()?.click());
    await flush();
    expect(board()).toBe("b2");
  });

  it("knows a get_todo result by its shape when the host names no tool", async () => {
    await mountFor(null, null, GET_TODO);
    expect(focused()).toBe(true);
  });

  it.each([
    ["get_board_summary", { board_id: "b1" }, { board: BOARD, lanes: [] }],
    [null, null, null],
  ])("shows the board alone when opened by %s", async (tool, input, result) => {
    await mountFor(tool, input, result);
    expect(drawerOpen()).toBe(false);
    expect(focused()).toBe(false);
    expect(container.textContent).toContain("Write the spec");
    expect(container.textContent).toContain("LAU-1");
  });

  it.each([
    ["its result", { board_id: "MAG" }, { board: OTHER, lanes: [] }],
    ["its input's key, before the result", { board_id: "mag" }, null],
  ])("selects the board get_board_summary summarized, by %s", async (_, input, result) => {
    await mountFor("get_board_summary", input, result);
    expect(board()).toBe("b2");

    // A pick from the selector afterwards stays, through a re-read.
    const select = container.querySelector<HTMLSelectElement>("select[aria-label=Board]")!;
    await act(async () => {
      select.value = "b1";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => dataSync.fire());
    await flush();
    expect(board()).toBe("b1");
  });

  it("opens a clicked card in a drawer whose breadcrumb's board closes it", async () => {
    await mountFor("get_board_summary", { board_id: "b1" }, { board: BOARD, lanes: [] });
    const card = [...container.querySelectorAll<HTMLElement>("*")]
      .filter((el) => el.textContent === "Write the spec")
      .at(-1);
    await act(async () => card?.click());
    await flush();
    expect(drawerOpen()).toBe(true);
    expect(container.textContent).toContain("Write the spec");
    expect(document.querySelector("[role=dialog]")?.textContent).toContain("Activity");
    // The to-do get_todo answers with lives on the other board: its name leads the way there.
    expect(crumb()?.textContent).toBe("Magnificent/MAG-3");

    await act(async () => backLink()?.click());
    await flush();
    expect(drawerOpen()).toBe(false);
    expect(board()).toBe("b2");
  });
});

// The focused view lays itself out by the width it is given, not the viewport's: a main
// column beside a properties sidebar from 760px, one column below it.
describe("the focused view's layout, by its width", () => {
  const TODO = {
    ...row("t1", "Write the spec"),
    status: "in_progress",
    priority: "high",
    labels: ["launch", "docs"],
    details: "Confirm the README.",
    source: "agent",
  };
  const NOW = new Date();
  const GET_TODO = {
    todo: TODO,
    steps: [],
    counts: { steps: 0, steps_done: 0 },
    recent_activity: [
      { todo_id: "t1", action: "start", outcome: "ok", at: NOW.toISOString() },
      { todo_id: "t1", action: "create", outcome: "ok", at: "2025-11-13T12:00:00Z" },
    ],
  };
  const realRect = Element.prototype.getBoundingClientRect;

  async function mountAt(width: number) {
    Element.prototype.getBoundingClientRect = function () {
      return { width, height: 600, top: 0, left: 0, right: width, bottom: 600, x: 0, y: 0, toJSON() {} };
    };
    appStub.toolInfo = { tool: { name: "get_todo" } };
    launch.input = { todo_id: "LAU-1" };
    launch.result = { structuredContent: GET_TODO };
    callToolSpy.mockReset();
    callToolSpy.mockImplementation(async (name: string) => {
      if (name === "list_boards") return { data: { boards: [BOARD] } };
      if (name === "get_todo") return { data: GET_TODO };
      return { data: {} };
    });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root.render(createElement(App)));
    await flush();
  }

  afterEach(() => {
    Element.prototype.getBoundingClientRect = realRect;
  });

  const buttons = () => [...container.querySelectorAll("button")].map((b) => b.textContent);
  const sidebar = () => container.querySelector("aside[aria-label=Properties]");

  it("puts the properties and every action in a sidebar when wide", async () => {
    await mountAt(900);
    const aside = sidebar();
    expect(aside).not.toBeNull();
    expect(aside?.textContent).toContain("Launch");
    // In progress: complete is primary; the rest are listed, never a move onto itself.
    const actions = [...aside!.querySelectorAll("button")].map((b) => b.textContent);
    expect(actions).toEqual(["Complete", "Block", "Edit", "Archive", "Reopen", "Cancel"]);
    expect(container.querySelector("button[aria-label='More actions']")).toBeNull();
  });

  it("is one column with one primary action and the rest behind ··· when narrow", async () => {
    await mountAt(500);
    expect(sidebar()).toBeNull();
    expect(buttons()).toContain("Complete");
    expect(buttons()).not.toContain("Block");
    const more = container.querySelector<HTMLButtonElement>("button[aria-label='More actions']");
    expect(more?.getAttribute("aria-expanded")).toBe("false");
    await act(async () => more?.click());
    expect(more?.getAttribute("aria-expanded")).toBe("true");
    expect(buttons()).toEqual(
      expect.arrayContaining(["Block", "Edit", "Archive", "Reopen", "Cancel"]),
    );
    expect(buttons()).not.toContain("Start");
  });

  it.each([500, 900])("shows status as a dot and text, and activity as plain sentences (%ipx)", async (width) => {
    await mountAt(width);
    const dot = [...container.querySelectorAll("[data-status-dot=in_progress]")].find((d) => !d.closest("li"));
    expect(dot?.parentElement?.textContent).toBe("In progress");
    const lines = [...container.querySelectorAll("li")].map((l) => l.firstElementChild?.nextElementSibling?.textContent);
    expect(lines).toEqual(["Moved to In progress", "Created on Launch"]);
    expect(container.textContent).not.toMatch(/IN PROGRESS|STARTED|agent/);
  });

  it("opens on one top bar: the breadcrumb, then the actions, then the title (narrow)", async () => {
    await mountAt(500);
    const crumb = container.querySelector("nav[aria-label=Breadcrumb]");
    expect(crumb?.textContent).toBe("Launch/LAU-1");
    const more = container.querySelector("button[aria-label='More actions']");
    expect(crumb?.parentElement?.contains(more)).toBe(true);
    expect(container.textContent?.match(/LAU-1/g)).toHaveLength(1);
  });

  it("puts the breadcrumb atop the main column and the key nowhere else (wide)", async () => {
    await mountAt(900);
    const crumb = container.querySelector("nav[aria-label=Breadcrumb]");
    expect(crumb?.textContent).toBe("Launch/LAU-1");
    expect(crumb?.parentElement?.firstElementChild).toBe(crumb);
    expect(container.textContent?.match(/LAU-1/g)).toHaveLength(1);
  });

  /** The muted line under the title, as its parts. */
  const metaLine = () => {
    const title = container.querySelector("h1");
    return (title?.nextElementSibling?.textContent ?? "").split("·");
  };

  it("draws one meta line under the title: status, priority when not normal, labels", async () => {
    await mountAt(500);
    expect(metaLine()).toEqual(["In progress", "High", "launchdocs"]);
  });

  it("leaves normal priority out of the meta line", async () => {
    GET_TODO.todo = { ...TODO, priority: "normal", labels: ["launch"] };
    try {
      await mountAt(500);
      expect(metaLine()).toEqual(["In progress", "launch"]);
      expect(container.textContent).not.toContain("Normal");
    } finally {
      GET_TODO.todo = TODO;
    }
  });
});

describe("the primary action for each status", () => {
  it.each([
    ["open", "Start"],
    ["in_progress", "Complete"],
    ["blocked", "Start"],
    ["done", "Reopen"],
    ["cancelled", "Reopen"],
  ])("is the most likely next move from %s", async (status, label) => {
    const todo = { ...row("t1", "Write the spec"), status, details: "" };
    appStub.toolInfo = { tool: { name: "get_todo" } };
    launch.input = null;
    launch.result = {
      structuredContent: { todo, steps: [], counts: { steps: 0, steps_done: 0 }, recent_activity: [] },
    };
    callToolSpy.mockReset();
    callToolSpy.mockImplementation(async (name: string) => {
      if (name === "list_boards") return { data: { boards: [BOARD] } };
      if (name === "get_todo") return { data: launch.result!.structuredContent };
      return { data: {} };
    });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root.render(createElement(App)));
    await flush();
    const more = container.querySelector("button[aria-label='More actions']");
    expect(more?.previousElementSibling?.textContent).toBe(label);
  });
});

// A card: the title, a blocked to-do's reason, and one meta line of key, priority and labels.
describe("a card", () => {
  const mount = async (element: ReturnType<typeof createElement>) => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root.render(element));
  };

  it.each([
    ["low", "Low priority"],
    ["high", "High priority"],
    ["urgent", "Urgent"],
  ])("draws a named glyph for %s priority", async (priority, name) => {
    await mount(createElement(PriorityIcon, { priority }));
    const icon = container.querySelector("[role=img]");
    expect(icon?.getAttribute("aria-label")).toBe(name);
    expect(icon?.getAttribute("title")).toBe(name);
    const fills = [...container.querySelectorAll("rect")].map((r) => r.getAttribute("fill"));
    if (priority === "low") expect(new Set(fills.slice(1)).has(fills[0])).toBe(false);
    if (priority === "high") expect(new Set(fills).size).toBe(1);
  });

  it.each(["normal", "someday"])("draws nothing for %s priority", async (priority) => {
    await mount(createElement(PriorityIcon, { priority }));
    expect(container.innerHTML).toBe("");
  });

  const LABELS = ["launch", "email", "docs", "growth", "legal"];
  const named = () =>
    [...container.querySelectorAll("span")].filter((s) => LABELS.includes(s.textContent ?? ""));

  it("shows two labels and collapses the rest into a +N button naming them", async () => {
    await mount(createElement(CardMeta, { todoKey: "LAU-5", priority: "normal", labels: LABELS }));
    expect(named().map((s) => s.textContent)).toEqual(["launch", "email"]);
    const more = container.querySelector("button");
    expect(more?.textContent).toBe("+3");
    expect(more?.getAttribute("title")).toBe("docs, growth, legal");
    expect(more?.getAttribute("aria-label")).toBe("3 more labels: docs, growth, legal");
  });

  it("drops to one label when two still overflow the line", async () => {
    const scroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollWidth");
    Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
      configurable: true,
      get() {
        // Only the row with the key and two labels is too wide.
        return this.querySelectorAll?.(":scope > span").length > 2 ? 500 : 0;
      },
    });
    try {
      await mount(createElement(CardMeta, { todoKey: "LAU-5", priority: "normal", labels: LABELS }));
    } finally {
      if (scroll) Object.defineProperty(HTMLElement.prototype, "scrollWidth", scroll);
      else delete (HTMLElement.prototype as { scrollWidth?: number }).scrollWidth;
    }
    expect(named().map((s) => s.textContent)).toEqual(["launch"]);
    expect(container.querySelector("button")?.getAttribute("title")).toBe("email, docs, growth, legal");
  });

  it("shows a blocked reason and no status or uppercase badge", async () => {
    const todo = {
      ...row("t1", "Publish the announcement"),
      status: "blocked",
      priority: "urgent",
      blocked_reason: "Waiting on approval",
    };
    await mount(
      createElement(TodoCard, { todo, onClick: () => {}, onDragStart: () => {}, dragging: false }),
    );
    expect(container.textContent).toContain("Waiting on approval");
    expect(container.textContent).not.toMatch(/Blocked|BLOCKED|URGENT|urgent/);
    expect(container.querySelector("[aria-label=Urgent]")).not.toBeNull();
  });
});

// A to-do's history: a rail of rows under day headings, newest first, one sentence each.
describe("the activity timeline", () => {
  const NOW = new Date();
  /** An ISO time `daysAgo` days back from today, at `hour`:`minute` local time. */
  const at = (daysAgo: number, hour: number, minute = 0) =>
    new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - daysAgo, hour, minute).toISOString();
  const act_ = (action: string, when: string, extra: object = {}) => ({
    todo_id: "t1",
    action,
    outcome: "ok",
    at: when,
    ...extra,
  });
  const sentences = (activity: object[], opts: object = { board: "Launch" }) =>
    activityEntries(activity as never, opts).map((e) => e.text + (e.emphasis ?? ""));

  const mount = async (activity: object[], createdAt?: string) => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () =>
      root.render(createElement(ActivityTimeline, { activity: activity as never, board: "Launch", createdAt })),
    );
  };
  const rows = () =>
    [...container.querySelectorAll("li")].map((l) => l.children[1]?.textContent ?? "");

  it("says one sentence per action, with a transition naming the status it moved to", () => {
    expect(
      sentences([
        act_("cancel", at(0, 9)),
        act_("reopen", at(0, 9)),
        act_("complete", at(0, 9)),
        act_("block", at(0, 9)),
        act_("start", at(0, 9)),
        act_("archive", at(0, 9)),
        act_("update", at(0, 9)),
        act_("snooze_until", at(0, 9)),
        act_("create", at(0, 8)),
      ]),
    ).toEqual([
      "Moved to Cancelled",
      "Moved to Open",
      "Moved to Done",
      "Moved to Blocked",
      "Moved to In progress",
      "Archived",
      "Edited",
      "Snooze until",
      "Created on Launch",
    ]);
  });

  it("names the edited fields as a sentence joins them", () => {
    expect(joinNatural(["the title"])).toBe("the title");
    expect(editedSentence({ fields: ["title"] })).toBe("Edited the title");
    expect(editedSentence({ fields: ["title", "details"] })).toBe("Edited the title and details");
    expect(editedSentence({ fields: ["title", "details", "labels"] })).toBe(
      "Edited the title, details and labels",
    );
    expect(editedSentence({ fields: ["due_at", "some_field"] })).toBe("Edited the due date and some field");
    expect(editedSentence(null)).toBe("Edited");
  });

  it("shows only what changed: no denied or failed attempts, matched creates, empty plans or reorders", () => {
    expect(
      sentences([
        act_("start", at(0, 10), { outcome: "denied" }),
        act_("complete", at(0, 10), { outcome: "error" }),
        act_("reorder", at(0, 10), { detail: { count: 3 } }),
        act_("create", at(0, 10), { detail: { matched: true } }),
        act_("plan", at(0, 10), { detail: { items: 2, created: 0 } }),
        act_("plan", at(0, 9), { detail: { items: 3, created: 3 } }),
        act_("create", at(0, 8)),
      ]),
    ).toEqual(["Added 3 steps", "Created on Launch"]);
    const [plan] = activityEntries([act_("plan", at(0, 9), { detail: { items: 3, created: 3 } })] as never);
    expect(plan.note).toBe("from a plan of 3 to-dos");
  });

  it("adds the creation a planned to-do has no row for, only when the whole history is here", () => {
    const created = at(2, 8);
    expect(sentences([act_("start", at(0, 9))], { board: "Launch", createdAt: created })).toEqual([
      "Moved to In progress",
      "Created on Launch",
    ]);
    const full = Array.from({ length: 20 }, () => act_("update", at(0, 9)));
    expect(sentences(full, { createdAt: created })).not.toContain("Created");
  });

  it("groups rows under Today, Yesterday and the date, newest first, each with its time", async () => {
    const older = at(5, 15, 40);
    await mount([act_("start", at(0, 14, 55)), act_("reopen", at(0, 14, 52)), act_("update", at(1, 16, 10)), act_("create", older)]);
    const headings = [...container.querySelectorAll("ol")].map(
      (ol) => ol.previousElementSibling?.textContent,
    );
    const dated = new Date(older).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const olderLabel =
      new Date(older).getFullYear() === NOW.getFullYear()
        ? dated
        : new Date(older).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    expect(headings).toEqual(["Today", "Yesterday", olderLabel]);
    expect(rows()).toEqual(["Moved to In progress", "Moved to Open", "Edited", "Created on Launch"]);
    const times = [...container.querySelectorAll("time")].map((t) => t.textContent);
    expect(times[0]).toBe(
      new Date(at(0, 14, 55)).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }),
    );
    // A move draws its destination's status dot on the rail; a reopen, open's ring.
    expect(container.querySelector("li [data-status-dot=in_progress]")).not.toBeNull();
    expect(container.querySelector("li [data-status-dot=open]")).not.toBeNull();
  });

  it("shows the newest eight, then a Show all button with the count", async () => {
    const many = Array.from({ length: 11 }, (_, i) => act_("update", at(0, 20 - i)));
    await mount([...many, act_("create", at(1, 9))]);
    expect(rows()).toHaveLength(8);
    const all = [...container.querySelectorAll("button")].find((b) => b.textContent === "Show all 12");
    expect(all).toBeDefined();
    await act(async () => all?.click());
    expect(rows()).toHaveLength(12);
    expect(rows().at(-1)).toBe("Created on Launch");
    expect(container.textContent).not.toContain("Show all");
  });

  it("says so when there is nothing to show", async () => {
    await mount([act_("start", at(0, 9), { outcome: "denied" })]);
    expect(container.textContent).toBe("No activity yet.");
  });
});

describe("toApiError", () => {
  it("reads the server's error shape", () => {
    expect(
      toApiError({
        error: { code: "not_found", message: "No todo", retryable: false, entity: "todo", id: "td_1" },
        next_step: "Verify the todo id.",
      }),
    ).toEqual({
      error: "not_found",
      message: "No todo",
      next_step: "Verify the todo id.",
      entity: "todo",
      id: "td_1",
    });
  });

  it("reads plain text, such as a schema rejection, as the message", () => {
    expect(toApiError("Input validation error")).toEqual({
      error: "error",
      message: "Input validation error",
    });
  });
});

// The search box lists to-dos with `list_todos` and its `query`, on the selected board, once
// the typing pauses.
describe("the search box", () => {
  it("lists the query's matches through list_todos", async () => {
    await mountBoard({
      todos: [row("t1", "Write the spec")],
      boardsReply: async () => ({ boards: [BOARD] }),
    });
    const respond = callToolSpy.getMockImplementation()!;
    callToolSpy.mockImplementation(async (name: string, args?: { query?: string }) =>
      name === "list_todos" && args?.query
        ? { data: { todos: [row("t9", "Ship the release", 9)], next_cursor: null } }
        : respond(name, args),
    );

    const input = container.querySelector<HTMLInputElement>("input[placeholder='Search to-dos…']")!;
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    await act(async () => {
      setValue.call(input, "ship");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });
    await flush();

    expect(callToolSpy).toHaveBeenCalledWith("list_todos", {
      query: "ship",
      board_id: BOARD.id,
      limit: 50,
    });
    expect(container.textContent).toContain("Ship the release");
  });
});
