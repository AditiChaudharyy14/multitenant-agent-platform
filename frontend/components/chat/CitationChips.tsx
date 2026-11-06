"use client";

import { humanizeSection } from "@/lib/brand";
import type { Citation } from "@/lib/types";

export function CitationChips({ citations, onOpen }: { citations: Citation[]; onOpen: (c: Citation, el: HTMLElement) => void }) {
  if (!citations.length) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-2" aria-label="Sources">
      {citations.map((c, i) => {
        const section = humanizeSection(c.id.split("#").pop() ?? "");
        return (
          <button
            key={c.id}
            type="button"
            data-testid="citation-chip"
            title={`${c.title}: ${section}`}
            onClick={(e) => onOpen(c, e.currentTarget)}
            className="group flex max-w-full items-center gap-2 rounded-full border border-rule bg-white py-1 pl-1 pr-3 text-left text-[13px] hover:border-brand"
          >
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand-soft text-[11px] font-semibold text-brand group-hover:bg-brand group-hover:text-white">
              {i + 1}
            </span>
            <span className="truncate">
              <span className="font-medium">{section}</span>
              <span className="text-muted"> in {c.title}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
