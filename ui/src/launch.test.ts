import { describe, expect, it } from "vitest";
import { detailTarget } from "./launch";

const RESULT = {
  todo: { id: "td_1", key: "MAG-3", board_id: "bd_1" },
  steps: [],
  counts: { steps: 0, steps_done: 0 },
  recent_activity: [],
};

describe("detailTarget", () => {
  it("opens the to-do a get_todo result carries, on its board", () => {
    expect(detailTarget("get_todo", { todo_id: "MAG-3" }, RESULT)).toEqual({
      todoId: "td_1",
      boardId: "bd_1",
    });
  });

  it("falls back to the input's id or key until the result arrives, or when it is an error", () => {
    expect(detailTarget("get_todo", { todo_id: " MAG-3 " }, null)).toEqual({ todoId: "MAG-3" });
    expect(
      detailTarget("get_todo", { todo_id: "MAG-3" }, { error: "not_found", next_step: "x" }),
    ).toEqual({ todoId: "MAG-3" });
    const flagged = { error: { code: "not_found", message: "m", retryable: false }, next_step: "x" };
    expect(detailTarget("get_todo", { todo_id: "MAG-3" }, flagged)).toEqual({ todoId: "MAG-3" });
  });

  it("opens nothing for another tool, or for no tool and no to-do result", () => {
    expect(detailTarget("get_board_summary", { board_id: "bd_1" }, RESULT)).toBeNull();
    expect(detailTarget(undefined, null, null)).toBeNull();
    expect(detailTarget(undefined, { todo_id: "MAG-3" }, { board: {}, lanes: [] })).toBeNull();
    expect(detailTarget("get_todo", {}, null)).toBeNull();
  });
});
