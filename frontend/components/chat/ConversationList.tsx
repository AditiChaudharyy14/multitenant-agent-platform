"use client";

import { useEffect, useRef, useState } from "react";

import type { Conversation } from "@/lib/types";

interface Props {
  conversations: Conversation[] | null;
  activeId: string | null;
  open: boolean; // the drawer, at 768px and below
  onClose: () => void;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onLogout: () => void;
  userName: string;
}

/** A sidebar above 768px; a drawer over the chat at 768px and below. */
export function ConversationList({ conversations, activeId, open, onClose, onSelect, onNew, onRename, onDelete, onLogout, userName }: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<Conversation | null>(null);

  const list = (
    <nav aria-label="Chats" className="flex h-full min-h-0 flex-col gap-3 p-3">
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
        {conversations?.map((c) =>
          editing === c.id ? (
            <li key={c.id}>
              <RenameField
                initial={c.title}
                onDone={async (title) => {
                  setEditing(null);
                  if (title && title !== c.title) await onRename(c.id, title);
                }}
              />
            </li>
          ) : (
            <li key={c.id} data-testid="conversation-item" className="group flex items-center rounded-[var(--radius-control)] hover:bg-brand-soft">
              <button
                type="button"
                onClick={() => onSelect(c.id)}
                aria-current={c.id === activeId ? "page" : undefined}
                className={`min-w-0 flex-1 truncate rounded-[var(--radius-control)] px-3 py-2 text-left text-sm ${
                  c.id === activeId ? "bg-brand-soft font-medium text-brand" : "text-ink"
                }`}
              >
                {c.title}
              </button>
              {/* Shown on hover and focus on desktop; always shown on touch screens, which can't hover. */}
              <span className="flex shrink-0 pr-1 [@media(hover:hover)]:hidden [@media(hover:hover)]:group-focus-within:flex [@media(hover:hover)]:group-hover:flex">
                <IconButton label={`Rename ${c.title}`} onClick={() => setEditing(c.id)}>✎</IconButton>
                <IconButton label={`Delete ${c.title}`} onClick={() => setConfirming(c)}>🗑</IconButton>
              </span>
            </li>
          ),
        )}
      </ul>
      <div className="flex items-center justify-between gap-2 border-t border-rule pt-3 text-sm">
        <span className="min-w-0 truncate text-muted">{userName}</span>
        <button type="button" onClick={onLogout} className="shrink-0 rounded-[var(--radius-control)] px-2.5 py-1.5 font-medium hover:bg-brand-soft">
          Log out
        </button>
      </div>
    </nav>
  );

  return (
    <>
      <aside className="hidden w-64 shrink-0 border-r border-rule min-[769px]:block">{list}</aside>
      {open && (
        <div className="fixed inset-0 z-30 min-[769px]:hidden">
          <div className="absolute inset-0 bg-ink/25" onClick={onClose} aria-hidden />
          <div role="dialog" aria-modal="true" aria-label="Chats" className="drawer-in absolute inset-y-0 left-0 w-[85%] max-w-xs bg-paper shadow-xl">
            {list}
          </div>
        </div>
      )}
      {confirming && (
        <ConfirmDelete
          title={confirming.title}
          onCancel={() => setConfirming(null)}
          onConfirm={async () => {
            const c = confirming;
            setConfirming(null);
            await onDelete(c.id);
          }}
        />
      )}
    </>
  );
}

function IconButton({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" aria-label={label} title={label} onClick={onClick} className="h-8 w-8 rounded-[var(--radius-control)] text-[14px] text-muted hover:bg-white hover:text-ink">
      <span aria-hidden>{children}</span>
    </button>
  );
}

function RenameField({ initial, onDone }: { initial: string; onDone: (title: string) => void }) {
  const [value, setValue] = useState(initial);
  const ref = useRef<HTMLInputElement>(null);
  const finished = useRef(false); // Enter and the following blur must not both save
  useEffect(() => ref.current?.select(), []);
  const finish = (save: boolean) => {
    if (finished.current) return;
    finished.current = true;
    onDone(save ? value.trim() : "");
  };
  return (
    <input
      ref={ref}
      aria-label="Chat name"
      value={value}
      maxLength={80}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") finish(true);
        if (e.key === "Escape") finish(false);
      }}
      onBlur={() => finish(true)}
      className="w-full rounded-[var(--radius-control)] border border-brand bg-white px-3 py-2 text-sm outline-none"
    />
  );
}

function ConfirmDelete({ title, onCancel, onConfirm }: { title: string; onCancel: () => void; onConfirm: () => void }) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-ink/30" onClick={onCancel} aria-hidden />
      <div role="alertdialog" aria-modal="true" aria-labelledby="del-title" aria-describedby="del-body" className="relative w-full max-w-sm rounded-[var(--radius-bubble)] bg-paper p-5 shadow-xl">
        <h2 id="del-title" className="font-display text-[19px]">Delete this chat?</h2>
        <p id="del-body" className="mt-1.5 text-sm text-muted [overflow-wrap:anywhere]">
          “{title}” will disappear from your list. Orders and bookings made in it stay as they are.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <button ref={cancelRef} type="button" onClick={onCancel} className="rounded-[var(--radius-control)] border border-rule px-4 py-2 text-sm font-medium">
            Keep chat
          </button>
          <button type="button" onClick={onConfirm} className="rounded-[var(--radius-control)] bg-danger px-4 py-2 text-sm font-medium text-white">
            Delete chat
          </button>
        </div>
      </div>
    </div>
  );
}
