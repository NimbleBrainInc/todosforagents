import { useCallback } from "react";
import { ToolCallError } from "@nimblebrain/synapse";
import { useApp } from "@nimblebrain/synapse/react";
import { toApiError } from "./types";

/** The body of a failed result a bridge rejected with: its structured content, else its first
 * text block (JSON when the server wrote it). */
function rejectedBody(result: unknown): unknown {
  const r = (typeof result === "object" && result !== null ? result : {}) as {
    structuredContent?: unknown;
    content?: unknown;
  };
  if (r.structuredContent !== undefined && r.structuredContent !== null) return r.structuredContent;
  const first = Array.isArray(r.content)
    ? r.content.find((b): b is { type: "text"; text: string } => b?.type === "text" && typeof b.text === "string")
    : undefined;
  if (!first) return null;
  try {
    return JSON.parse(first.text);
  } catch {
    return first.text;
  }
}

/**
 * Returns a typed `call(toolName, args)` bound to the app's tool bridge.
 *
 * `app.callTool` returns a `ToolCallResult` whose `.data` is the tool's result (the first
 * text block, JSON-parsed). A To-dos failure is a result flagged `isError`; a bridge either
 * resolves with that flag or rejects with a `ToolCallError` carrying the result. Both come
 * back as an `ApiError` (`toApiError`), so callers inspect the returned value with
 * `isApiError` instead of catching. A genuine transport/auth failure still rejects; callers
 * may catch that.
 */
export function useCall(): <T>(name: string, args?: Record<string, unknown>) => Promise<T> {
  const app = useApp();
  return useCallback(
    async <T>(name: string, args?: Record<string, unknown>): Promise<T> => {
      try {
        const res = await app.callTool(name, args);
        return (res.isError ? toApiError(res.data) : res.data) as T;
      } catch (err) {
        if (err instanceof ToolCallError) return toApiError(rejectedBody(err.result)) as T;
        throw err;
      }
    },
    [app],
  );
}
