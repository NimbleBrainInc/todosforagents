import { tokens } from "@nimblebrain/synapse/ui";
import type { TodoRow } from "../types";
import { CardMeta } from "./marks";

/**
 * A draggable kanban card: the title (two lines at most), a blocked to-do's reason, and one
 * meta line (key, priority, labels). The lane says the status, so the card does not.
 *
 * Drag wiring (dragstart/dragover/drop) lives on the lane in BoardView so a single handler
 * owns both cross-lane (transition) and in-lane (reorder) drops; the card just exposes
 * itself as a drag source.
 */
export function TodoCard({
  todo,
  onClick,
  onDragStart,
  dragging,
}: {
  todo: TodoRow;
  onClick: () => void;
  onDragStart: () => void;
  dragging: boolean;
}) {
  return (
    <div
      draggable
      onDragStart={onDragStart}
      onClick={onClick}
      style={{
        cursor: "grab",
        opacity: dragging ? 0.4 : 1,
        background: tokens.bg,
        border: `1px solid ${tokens.border}`,
        borderRadius: 8,
        padding: "10px 12px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        minWidth: 0,
      }}
    >
      <div
        style={{
          fontSize: 13.5,
          fontWeight: 500,
          lineHeight: 1.35,
          color: tokens.fg,
          display: "-webkit-box",
          WebkitLineClamp: 2,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
          overflowWrap: "anywhere",
        }}
      >
        {todo.title}
      </div>
      {todo.status === "blocked" && todo.blocked_reason ? (
        <div
          style={{
            fontSize: 12,
            lineHeight: "16px",
            color: tokens.warning,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {todo.blocked_reason}
        </div>
      ) : null}
      <CardMeta todoKey={todo.key} priority={todo.priority} labels={todo.labels ?? []} />
    </div>
  );
}
