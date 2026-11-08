"use client";

import { CardView } from "@/components/cards/CardView";
import { CitationChips } from "@/components/chat/CitationChips";
import { Markdown, withCitationMarkers } from "@/components/chat/Markdown";
import type { Card, Citation, MessageStatus } from "@/lib/types";

export interface UiMessage {
  key: string;
  id?: string;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
  citations: Citation[];
  cards: Card[];
  error?: { code: string; message: string };
}

// Headlines for the error codes the agent can send; the body is the server's own message.
const ERROR_TITLES: Record<string, string> = {
  quota_exhausted: "Out of assistant messages for today",
  rate_limited: "The assistant is busy",
  network: "Connection problem",
};

interface Props {
  message: UiMessage;
  activity: string | null; // the live tool status line, while this message streams
  onOpenSource: (c: Citation, el: HTMLElement | null) => void;
}

export function MessageView({ message: m, activity, onOpenSource }: Props) {
  if (m.role === "user") {
    return (
      <div className="flex justify-end" data-role="user">
        <p className="max-w-[85%] whitespace-pre-wrap rounded-[var(--radius-bubble)] rounded-br-sm bg-brand px-4 py-2.5 text-white">
          {m.content}
        </p>
      </div>
    );
  }

  const streaming = m.status === "streaming";
  const text = withCitationMarkers(m.content, m.citations, streaming);
  const waiting = streaming && !m.content && !activity;

  return (
    <article data-role="assistant" data-status={m.status} data-testid="assistant-message" aria-busy={streaming} className="flex flex-col">
      {text && (
        <div className={streaming ? "streaming-tail" : undefined}>
          <Markdown
            text={text}
            onCite={(n) => {
              const c = m.citations[n - 1];
              if (c) onOpenSource(c, document.activeElement as HTMLElement | null);
            }}
          />
        </div>
      )}

      {activity && (
        <p data-testid="tool-activity" className="mt-1 flex items-center gap-2 text-sm text-muted">
          <span aria-hidden className="pulse-dot inline-block h-2 w-2 rounded-full bg-brand" />
          {activity}
        </p>
      )}
      {waiting && (
        <p className="flex items-center gap-1.5 text-muted" aria-label="The assistant is typing">
          {[0, 1, 2].map((i) => (
            <span key={i} className="pulse-dot inline-block h-1.5 w-1.5 rounded-full bg-muted" style={{ animationDelay: `${i * 150}ms` }} />
          ))}
        </p>
      )}

      {m.cards.map((card, i) => (
        <CardView key={i} card={card} />
      ))}

      <CitationChips citations={m.citations} onOpen={onOpenSource} />

      {m.status === "stopped" && (
        <p data-testid="stopped-tag" className="mt-2 self-start rounded-full border border-rule px-2.5 py-0.5 text-[12px] text-muted">
          Stopped
        </p>
      )}
      {m.status === "error" && (
        <div role="alert" className="mt-2 rounded-[var(--radius-control)] border border-danger/30 bg-white px-4 py-3 text-sm">
          <p className="font-medium text-danger">{ERROR_TITLES[m.error?.code ?? ""] ?? "This reply didn't finish"}</p>
          <p className="mt-0.5 text-muted">{m.error?.message ?? "Something went wrong while answering. Try asking again."}</p>
        </div>
      )}
    </article>
  );
}
