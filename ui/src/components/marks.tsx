import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactElement } from "react";
import { tokens } from "@nimblebrain/synapse/ui";
import { labelColor, priorityLabel, statusLabel } from "../format";

// The small marks a to-do is drawn with: a status dot, a priority glyph, and label dots.
// Text and greys come from the host's tokens so theming and dark mode follow the host; the
// indicator colours have no token and are fixed here.

/**
 * Status dot colours. Open is a hollow ring; the filled ones climb in lightness (in progress
 * L*42, blocked 57, done 70, cancelled 81), so they read apart without telling hues apart,
 * and each stays visible on a light or a dark surface.
 */
const STATUS_DOT: Record<string, string> = {
  open: "#8A8A8A",
  in_progress: "#2B5FBF",
  blocked: "#EA580C",
  done: "#22C55E",
  cancelled: "#C9C9C5",
};

/** The colour a status is drawn in: its dot, and the lane header that shows it. */
export function statusColor(status: string): string {
  return STATUS_DOT[status] ?? STATUS_DOT.open;
}

/** A status dot. Decorative: the text beside it, or the lane name, says the status. */
export function StatusMark({ status, size = 8 }: { status: string; size?: number }) {
  const color = statusColor(status);
  const hollow = !(status in STATUS_DOT) || status === "open";
  return (
    <span
      aria-hidden="true"
      data-status-dot={status}
      style={{
        display: "inline-block",
        flexShrink: 0,
        width: size,
        height: size,
        boxSizing: "border-box",
        borderRadius: "50%",
        background: hollow ? "transparent" : color,
        border: hollow ? `1.5px solid ${color}` : "none",
      }}
    />
  );
}

/** A status as a dot and sentence-case text ("In progress"). */
export function StatusText({ status, style }: { status: string; style?: CSSProperties }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, ...style }}>
      <StatusMark status={status} />
      {statusLabel(status)}
    </span>
  );
}

/** Urgent's fill. */
const URGENT = "#C2410C";

/**
 * A priority glyph: low is signal bars with the first dark, high all three dark, urgent a
 * filled square with "!". Normal, and any priority this does not know, draws nothing.
 * Named ("High priority") with a matching tooltip, unless `decorative` because a word
 * beside it says the priority already.
 */
export function PriorityIcon({
  priority,
  decorative = false,
}: {
  priority: string;
  decorative?: boolean;
}) {
  let glyph: ReactElement;
  if (priority === "low" || priority === "high") {
    const on = tokens.fg;
    const off = tokens.borderStrong;
    const first = priority === "low" ? tokens.fgMuted : on;
    const rest = priority === "low" ? off : on;
    glyph = (
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <rect x="1" y="8" width="3" height="5" rx="1" fill={first} />
        <rect x="5.5" y="5" width="3" height="8" rx="1" fill={rest} />
        <rect x="10" y="2" width="3" height="11" rx="1" fill={rest} />
      </svg>
    );
  } else if (priority === "urgent") {
    glyph = (
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <rect x="1" y="1" width="12" height="12" rx="3" fill={URGENT} />
        <rect x="6.25" y="3.5" width="1.5" height="5" fill="#FFFFFF" />
        <rect x="6.25" y="9.75" width="1.5" height="1.5" fill="#FFFFFF" />
      </svg>
    );
  } else {
    return null;
  }
  const name = priority === "urgent" ? "Urgent" : `${priorityLabel(priority)} priority`;
  return (
    <span
      data-priority={priority}
      {...(decorative ? { "aria-hidden": true } : { role: "img", "aria-label": name, title: name })}
      style={{ display: "inline-flex", flexShrink: 0, lineHeight: 0 }}
    >
      {glyph}
    </span>
  );
}

/** A priority as its glyph and word ("High"); normal is the word alone. */
export function PriorityText({ priority }: { priority: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <PriorityIcon priority={priority} decorative />
      {priorityLabel(priority)}
    </span>
  );
}

/** A label as a coloured dot and its name. */
export function LabelTag({ name }: { name: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, whiteSpace: "nowrap" }}>
      <span
        aria-hidden="true"
        style={{
          width: 6,
          height: 6,
          flexShrink: 0,
          borderRadius: "50%",
          background: labelColor(name),
        }}
      />
      {name}
    </span>
  );
}

/** Every label, wrapping: the detail's view of them. */
export function LabelList({ labels }: { labels: string[] }) {
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", alignItems: "center", gap: "4px 10px" }}>
      {labels.map((l) => (
        <LabelTag key={l} name={l} />
      ))}
    </span>
  );
}

/** The most labels a card shows by name before the rest collapse into `+N`. */
export const CARD_LABELS = 2;

/**
 * A card's one-line meta row: the key, the priority glyph, then up to two labels and a `+N`
 * chip naming the rest. The row never wraps; when two labels still overflow it, it shows one.
 */
export function CardMeta({
  todoKey,
  priority,
  labels,
}: {
  todoKey: string;
  priority: string;
  labels: string[];
}) {
  const row = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(CARD_LABELS);
  const labelKey = labels.join("\n");

  // Start from two labels whenever they change, then drop to one if the row overflows.
  useLayoutEffect(() => {
    setShown(CARD_LABELS);
  }, [labelKey]);
  useLayoutEffect(() => {
    const el = row.current;
    if (el && shown > 1 && labels.length > 1 && el.scrollWidth > el.clientWidth) {
      setShown(1);
    }
  });

  const visible = labels.slice(0, shown);
  const hidden = labels.slice(shown);
  return (
    <div
      ref={row}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        fontSize: 12,
        lineHeight: "16px",
        color: tokens.fgMuted,
        whiteSpace: "nowrap",
        overflow: "hidden",
      }}
    >
      <span style={{ fontFamily: tokens.fontMono, fontSize: 11.5 }}>{todoKey}</span>
      <PriorityIcon priority={priority} />
      {visible.map((l) => (
        <LabelTag key={l} name={l} />
      ))}
      {hidden.length ? (
        <button
          type="button"
          title={hidden.join(", ")}
          aria-label={`${hidden.length} more ${hidden.length === 1 ? "label" : "labels"}: ${hidden.join(", ")}`}
          style={{
            font: "inherit",
            fontSize: 11.5,
            color: tokens.fgMuted,
            // Raised, not bgSubtle: tertiary is unset in the host dark theme.
            background: tokens.bgRaised,
            border: 0,
            borderRadius: 6,
            padding: "1px 6px",
            cursor: "pointer",
            flexShrink: 0,
          }}
        >
          +{hidden.length}
        </button>
      ) : null}
    </div>
  );
}
