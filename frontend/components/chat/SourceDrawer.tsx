"use client";

import { useEffect, useRef, useState } from "react";

import { Markdown } from "@/components/chat/Markdown";
import { api, ApiError } from "@/lib/api";
import { humanizeSection } from "@/lib/brand";
import type { Citation, KbSource } from "@/lib/types";

/** The cited help-page passage, fetched by ref. The backend scopes it to the session's org. */
export function SourceDrawer({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const [source, setSource] = useState<KbSource | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let live = true;
    setSource(null);
    setError(null);
    api
      .kbSource(citation.id)
      .then((s) => live && setSource(s))
      .catch((err) => live && setError(err instanceof ApiError && err.status === 404 ? "This source is no longer available." : "Couldn't load this source. Try again."));
    return () => {
      live = false;
    };
  }, [citation.id]);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const section = humanizeSection(citation.id.split("#").pop() ?? "");
  return (
    <div className="fixed inset-0 z-40 flex items-end md:items-stretch md:justify-end">
      <div className="absolute inset-0 bg-ink/25" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="source-title"
        data-testid="source-drawer"
        className="drawer-in relative flex max-h-[85dvh] w-full flex-col rounded-t-[var(--radius-bubble)] bg-paper shadow-xl md:max-h-none md:w-[440px] md:rounded-none md:border-l md:border-rule"
      >
        <header className="flex items-start gap-3 border-b border-rule px-5 py-4">
          <div className="min-w-0 flex-1">
            <p className="text-[13px] text-muted">{source?.doc_title ?? citation.title}</p>
            <h2 id="source-title" className="mt-0.5 text-[20px] font-semibold leading-snug">
              {section}
            </h2>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close source"
            className="rounded-[var(--radius-control)] px-2 py-1 text-xl leading-none text-muted hover:bg-brand-soft hover:text-ink"
          >
            ×
          </button>
        </header>
        <div className="overflow-y-auto px-5 py-5 text-[15px]">
          {error && <p className="text-danger">{error}</p>}
          {!error && !source && <p className="text-muted">Loading the source…</p>}
          {source && (
            <div data-testid="source-content">
              <Markdown text={source.content} />
            </div>
          )}
          <p className="mt-6 border-t border-rule pt-3 text-[13px] text-muted">
            From our help pages. Prices, stock and availability always come from live data, never from here.
          </p>
        </div>
      </aside>
    </div>
  );
}
