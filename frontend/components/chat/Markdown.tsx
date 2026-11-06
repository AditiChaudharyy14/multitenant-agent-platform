"use client";

import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import type { Citation } from "@/lib/types";

// A citation group as the model writes it: [faq#returns] or [faq#a, faq#b].
const ID = String.raw`[A-Za-z0-9][\w.-]*#[\w.-]+`;
const GROUP = new RegExp(String.raw`\s*\[(${ID}(?:\s*[,;]\s*${ID})*)\]`, "g");
const PARTIAL_TAIL = /\s*\[[^\]\s]*$/; // a group still arriving token by token

/**
 * Turns [doc#section] into numbered markers that link to the chips below the message.
 * Ids that aren't (yet) confirmed citations are hidden: while streaming, the server
 * hasn't checked them; once done, the server has already removed any it rejected.
 */
export function withCitationMarkers(text: string, citations: Citation[], streaming: boolean): string {
  let out = text.replace(GROUP, (_m, group: string) => {
    const nums = group
      .split(/\s*[,;]\s*/)
      .map((id) => citations.findIndex((c) => c.id === id) + 1)
      .filter((n) => n > 0);
    return nums.map((n) => ` [${n}](#cite-${n})`).join("");
  });
  if (streaming) out = out.replace(PARTIAL_TAIL, "");
  return out;
}

export function Markdown({ text, onCite }: { text: string; onCite?: (n: number) => void }) {
  return (
    <div className="prose-chat">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        components={{
          a({ href, children }) {
            if (href?.startsWith("#cite-")) {
              const n = Number(href.slice(6));
              return (
                <button
                  type="button"
                  onClick={() => onCite?.(n)}
                  aria-label={`Open source ${n}`}
                  className="mx-0.5 inline-flex h-[1.35em] min-w-[1.35em] -translate-y-px items-center justify-center rounded-full bg-brand-soft px-1 align-middle text-[0.72em] font-semibold text-brand ring-1 ring-brand/25 hover:bg-brand hover:text-white"
                >
                  {children}
                </button>
              );
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
