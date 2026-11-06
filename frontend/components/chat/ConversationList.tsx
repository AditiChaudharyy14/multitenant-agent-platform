"use client";

import type { Conversation } from "@/lib/types";

interface Props {
  conversations: Conversation[] | null;
  activeId: string | null;
  open: boolean; // the mobile sheet
  onClose: () => void;
  onSelect: (id: string) => void;
  onNew: () => void;
}

/** A sidebar on desktop; a sheet over the chat on mobile. */
export function ConversationList({ conversations, activeId, open, onClose, onSelect, onNew }: Props) {
  const list = (
    <nav aria-label="Chats" className="flex h-full flex-col gap-3 p-3">
      <button
        type="button"
        onClick={onNew}
        className="rounded-[var(--radius-control)] border border-rule bg-white px-3 py-2 text-left text-sm font-medium hover:border-brand"
      >
        + New chat
      </button>
      <ul className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto">
        {conversations === null && <li className="px-3 py-2 text-sm text-muted">Loading…</li>}
        {conversations?.length === 0 && <li className="px-3 py-2 text-sm text-muted">Your chats will appear here.</li>}
        {conversations?.map((c) => (
          <li key={c.id}>
            <button
              type="button"
              onClick={() => onSelect(c.id)}
              aria-current={c.id === activeId ? "page" : undefined}
              className={`w-full truncate rounded-[var(--radius-control)] px-3 py-2 text-left text-sm ${
                c.id === activeId ? "bg-brand-soft font-medium text-brand" : "text-ink hover:bg-brand-soft"
              }`}
            >
              {c.title}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );

  return (
    <>
      <aside className="hidden w-64 shrink-0 border-r border-rule md:block">{list}</aside>
      {open && (
        <div className="fixed inset-0 z-30 md:hidden">
          <div className="absolute inset-0 bg-ink/25" onClick={onClose} aria-hidden />
          <div role="dialog" aria-modal="true" aria-label="Chats" className="drawer-in absolute inset-y-0 left-0 w-[80%] max-w-xs bg-paper shadow-xl">
            {list}
          </div>
        </div>
      )}
    </>
  );
}
