import { useState, type CSSProperties, type ReactNode } from "react";
import { Button, Heading, Inline, Prose, Stack, Text, tokens } from "@nimblebrain/synapse/ui";
import type { TodoActivity } from "../types";
import {
  activityEntries,
  fmtDateTime,
  fmtTime,
  groupByDay,
  markdownToHtml,
  type ActivityNode,
} from "../format";
import { StatusMark } from "./marks";

const inputStyle: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  fontFamily: tokens.fontSans,
  fontSize: tokens.textSmSize,
  lineHeight: tokens.textSmLine,
  color: tokens.fg,
  background: tokens.bg,
  border: `1px solid ${tokens.border}`,
  borderRadius: tokens.radiusSm,
  padding: "0.45rem 0.6rem",
};

/** A labeled text input, textarea, or select for forms. */
export function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  required = false,
  textarea = false,
  rows = 4,
  autoFocus = false,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
  required?: boolean;
  textarea?: boolean;
  rows?: number;
  autoFocus?: boolean;
  /** When set, render a <select> with these options instead of an input. */
  options?: { label: string; value: string }[];
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
      <Text size="xs" tone="muted" weight="medium">
        {label}
        {required ? <span style={{ color: tokens.danger }}> *</span> : null}
      </Text>
      {options ? (
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoFocus={autoFocus}
          style={inputStyle}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      ) : textarea ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          rows={rows}
          autoFocus={autoFocus}
          style={{ ...inputStyle, resize: "vertical" }}
        />
      ) : (
        <input
          type={type}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          autoFocus={autoFocus}
          style={inputStyle}
        />
      )}
    </label>
  );
}

/** A centered modal overlay used by the create/edit/transition forms. */
export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 50,
        background: "rgba(15, 23, 42, 0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "clamp(0.75rem, 4vw, 1.5rem)",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        style={{
          width: "min(440px, 100%)",
          maxHeight: "90vh",
          overflowY: "auto",
          background: tokens.bg,
          border: `1px solid ${tokens.border}`,
          borderRadius: tokens.radiusMd,
          boxShadow: "0 20px 60px rgba(15, 23, 42, 0.25)",
          padding: "1.25rem",
        }}
      >
        <Stack gap="1rem">
          <Heading size="sm">{title}</Heading>
          {children}
          <Inline gap="0.5rem" justify="end">
            {footer ?? (
              <Button variant="ghost" onClick={onClose}>
                Close
              </Button>
            )}
          </Inline>
        </Stack>
      </div>
    </div>
  );
}

/** How many history rows show before "Show all". */
export const ACTIVITY_SHOWN = 8;

/** The rail's mark for a row: a status dot for a move, a pencil for an edit, a filled "+" for
 * a creation, a small grey dot for anything else. */
function RailNode({ node }: { node: ActivityNode }) {
  const disc = (background: string, children: ReactNode) => (
    <span
      aria-hidden="true"
      style={{
        width: 18,
        height: 18,
        flexShrink: 0,
        borderRadius: "50%",
        background,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {children}
    </span>
  );
  switch (node.kind) {
    case "status":
      return (
        <span style={{ marginTop: 4, lineHeight: 0 }}>
          <StatusMark status={node.status} size={10} />
        </span>
      );
    case "edit":
      return disc(
        tokens.bgRaised,
        <svg width="10" height="10" viewBox="0 0 10 10">
          <path
            d="M1.5 8.5 2 6.5 6.5 2l1.5 1.5L3.5 8z"
            fill="none"
            stroke={tokens.fgMuted}
            strokeWidth="1.2"
            strokeLinejoin="round"
          />
        </svg>,
      );
    case "create":
      return disc(
        tokens.fg,
        <svg width="10" height="10" viewBox="0 0 10 10">
          <path d="M5 1.5v7M1.5 5h7" stroke={tokens.bg} strokeWidth="1.4" strokeLinecap="round" />
        </svg>,
      );
    default:
      return (
        <span
          aria-hidden="true"
          style={{
            width: 8,
            height: 8,
            marginTop: 5,
            flexShrink: 0,
            borderRadius: "50%",
            background: tokens.borderStrong,
          }}
        />
      );
  }
}

/**
 * A to-do's history as a rail timeline (`activityEntries`): rows grouped under day headings in
 * the viewer's local time, newest first, each a mark on the rail, a sentence and the time. The
 * newest `ACTIVITY_SHOWN` show, then a "Show all N" button.
 */
export function ActivityTimeline({
  activity,
  board,
  createdAt,
}: {
  activity: TodoActivity[];
  /** The name of the to-do's board, which its creation names. */
  board?: string;
  /** When the to-do was created, for a history whose creation row lives elsewhere. */
  createdAt?: string;
}) {
  const [all, setAll] = useState(false);
  const entries = activityEntries(activity, { board, createdAt });
  if (entries.length === 0) {
    return <div style={{ fontSize: 13, color: tokens.fgMuted }}>No activity yet.</div>;
  }
  const shown = all ? entries : entries.slice(0, ACTIVITY_SHOWN);
  const last = shown[shown.length - 1];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {groupByDay(shown).map((g) => (
        <div key={g.day} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ fontSize: 11.5, fontWeight: 600, color: tokens.fgMuted, letterSpacing: "0.02em" }}>
            {g.day}
          </div>
          <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column" }}>
            {g.entries.map((e, i) => (
              <li
                key={`${e.at}:${i}`}
                style={{
                  display: "grid",
                  gridTemplateColumns: "20px 1fr auto",
                  columnGap: 12,
                  alignItems: "stretch",
                }}
              >
                <div
                  style={{ display: "flex", flexDirection: "column", alignItems: "center" }}
                >
                  <RailNode node={e.node} />
                  {e === last ? null : (
                    <span
                      aria-hidden="true"
                      style={{ flex: 1, width: 2, minHeight: 14, marginTop: 4, background: tokens.border }}
                    />
                  )}
                </div>
                <div style={{ paddingBottom: e === last ? 0 : 16, minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, lineHeight: 1.45, color: tokens.fg }}>
                    {e.text}
                    {e.emphasis ? <span style={{ fontWeight: 500 }}>{e.emphasis}</span> : null}
                  </div>
                  {e.note ? (
                    <div style={{ fontSize: 12, color: tokens.fgMuted, marginTop: 2 }}>{e.note}</div>
                  ) : null}
                </div>
                <time
                  dateTime={e.at}
                  title={fmtDateTime(e.at)}
                  style={{ fontSize: 12, color: tokens.fgMuted, paddingTop: 1, whiteSpace: "nowrap" }}
                >
                  {fmtTime(e.at)}
                </time>
              </li>
            ))}
          </ol>
        </div>
      ))}
      {!all && entries.length > ACTIVITY_SHOWN ? (
        <button
          type="button"
          onClick={() => setAll(true)}
          style={{
            alignSelf: "flex-start",
            font: "inherit",
            fontSize: 12.5,
            padding: "4px 0",
            border: 0,
            background: "none",
            color: tokens.accent,
            cursor: "pointer",
          }}
        >
          Show all {entries.length}
        </button>
      ) : null}
    </div>
  );
}

/** Render a to-do's markdown details, or a muted placeholder. Sanitized (see `markdownToHtml`). */
export function DetailsBody({ markdown }: { markdown: string | null | undefined }) {
  if (!markdown) {
    return (
      <Text size="sm" tone="faint">
        No details.
      </Text>
    );
  }
  return <Prose html={markdownToHtml(markdown)} />;
}

/** A to-do's key (`MAG-3`), the name people and the agent use for it. Plain text, never HTML. */
export function TodoKey({ value, size = "xs" }: { value: string; size?: "xs" | "sm" }) {
  return (
    <Text size={size} weight="medium" tone="muted" mono>
      {value}
    </Text>
  );
}
