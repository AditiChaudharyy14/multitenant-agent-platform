// Reads the chat turn's Server-Sent Events from a POST response body. EventSource can't
// POST or be aborted per request, so this uses fetch + ReadableStream + AbortController.

import { ApiError } from "./api";
import type { Card, Citation } from "./types";

export interface DoneEvent {
  message_id: string | null;
  turn_id: string;
  status: "complete" | "stopped" | "error";
  content: string;
  citations: Citation[];
}

export interface StreamHandlers {
  token?: (text: string) => void;
  toolStart?: (e: { call_id: string; name: string; label: string }) => void;
  toolEnd?: (e: { call_id: string; name: string; label: string; ok: boolean }) => void;
  card?: (card: Card) => void;
  citation?: (c: Citation) => void;
  error?: (e: { code: string; message: string }) => void;
  done?: (e: DoneEvent) => void;
}

export type MessageBody =
  | { content: string }
  | { action: { type: string; [key: string]: unknown } };

export async function streamMessage(
  conversationId: string,
  body: MessageBody,
  on: StreamHandlers,
  signal: AbortSignal,
  base = "/api/conversations", // the admin analytics chat uses /api/admin/conversations
): Promise<void> {
  const res = await fetch(`${base}/${encodeURIComponent(conversationId)}/messages`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw await ApiError.from(res); // 429 carries Retry-After

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replace(/\r\n/g, "\n");
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      dispatch(buffer.slice(0, end), on);
      buffer = buffer.slice(end + 2);
    }
  }
  if (buffer.trim()) dispatch(buffer, on);
}

function dispatch(block: string, on: StreamHandlers): void {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue; // ": heartbeat" comments
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  if (!data.length) return;
  const payload = JSON.parse(data.join("\n"));
  switch (event) {
    case "token":
      return on.token?.(payload.text);
    case "tool_start":
      return on.toolStart?.(payload);
    case "tool_end":
      return on.toolEnd?.(payload);
    case "card":
      return on.card?.(payload.card);
    case "citation":
      return on.citation?.(payload);
    case "error":
      return on.error?.(payload);
    case "done":
      return on.done?.(payload);
  }
}
