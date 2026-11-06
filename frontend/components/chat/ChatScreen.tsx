"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { Composer } from "@/components/chat/Composer";
import { ConversationList } from "@/components/chat/ConversationList";
import { MessageView, type UiMessage } from "@/components/chat/MessageView";
import { SourceDrawer } from "@/components/chat/SourceDrawer";
import { OrgSeal } from "@/components/OrgSeal";
import { api, ApiError } from "@/lib/api";
import { streamMessage } from "@/lib/sse";
import { SUGGESTIONS } from "@/lib/suggestions";
import type { Citation, Conversation, Me, StoredMessage } from "@/lib/types";

type Load = "ready" | "loading" | "error" | "notfound";

const key = () => Math.random().toString(36).slice(2);

function fromStored(m: StoredMessage): UiMessage {
  return { key: m.id, id: m.id, role: m.role, content: m.content, status: m.status,
           citations: m.citations ?? [], cards: m.cards ?? [] }; // prettier-ignore
}

export function ChatScreen({ me, routeConversationId }: { me: Me; routeConversationId: string | null }) {
  const router = useRouter();
  const { org } = me;
  const slug = org.slug;
  const base = `/a/${slug}/chat`;

  const [conversationId, setConversationId] = useState<string | null>(routeConversationId);
  const convRef = useRef<string | null>(routeConversationId); // current id, readable inside async code
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [load, setLoad] = useState<Load>(routeConversationId ? "loading" : "ready");
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [activity, setActivity] = useState<string | null>(null);
  const [rateUntil, setRateUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [listOpen, setListOpen] = useState(false);
  const [source, setSource] = useState<Citation | null>(null);
  const sourceOpener = useRef<HTMLElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const refreshConversations = useCallback(() => {
    api.conversations().then(setConversations).catch(() => undefined);
  }, []);

  useEffect(refreshConversations, [refreshConversations]);

  const loadMessages = useCallback(async (id: string) => {
    setLoad("loading");
    setMessages([]);
    try {
      const stored = await api.messages(id);
      if (convRef.current !== id) return; // the user moved on meanwhile
      setMessages(stored.map(fromStored));
      setLoad("ready");
    } catch (err) {
      if (convRef.current !== id) return;
      setLoad(err instanceof ApiError && err.status === 404 ? "notfound" : "error");
    }
  }, []);

  /** Switch conversation (null = a new one). `push` adds a history entry. */
  const open = useCallback(
    (id: string | null, push: boolean) => {
      abortRef.current?.abort();
      convRef.current = id;
      setConversationId(id);
      setSource(null);
      setListOpen(false);
      setActivity(null);
      if (id) void loadMessages(id);
      else {
        setMessages([]);
        setLoad("ready");
      }
      if (push) window.history.pushState(null, "", id ? `${base}/${id}` : base);
    },
    [base, loadMessages],
  );

  // First load, and back/forward between conversations. A route id we already hold (the
  // one this screen just created) is not reloaded, so a stream in progress is untouched.
  useEffect(() => {
    if (routeConversationId !== convRef.current) open(routeConversationId, false);
    else if (routeConversationId && load === "loading" && messages.length === 0) void loadMessages(routeConversationId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeConversationId]);

  // The 429 countdown.
  useEffect(() => {
    if (rateUntil === null) return;
    const t = setInterval(() => {
      setNow(Date.now());
      if (Date.now() >= rateUntil) setRateUntil(null);
    }, 250);
    return () => clearInterval(t);
  }, [rateUntil]);

  // Keep the newest message in view.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, activity]);

  const patch = (k: string, f: (m: UiMessage) => UiMessage) =>
    setMessages((ms) => ms.map((m) => (m.key === k ? f(m) : m)));

  async function send(text: string) {
    text = text.trim();
    if (!text || streaming || rateUntil !== null) return;
    const userKey = key();
    const asstKey = key();
    setDraft("");
    setMessages((ms) => [
      ...ms,
      { key: userKey, role: "user", content: text, status: "complete", citations: [], cards: [] },
      { key: asstKey, role: "assistant", content: "", status: "streaming", citations: [], cards: [] },
    ]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setStreaming(true);
    let finished = false;
    try {
      let id = convRef.current;
      if (!id) {
        id = (await api.createConversation()).id;
        convRef.current = id;
        setConversationId(id);
        // Same page component for /chat and /chat/<id>: the URL changes, the screen stays.
        window.history.replaceState(null, "", `${base}/${id}`);
      }
      await streamMessage(
        id,
        { content: text },
        {
          token: (t) => {
            setActivity(null);
            patch(asstKey, (m) => ({ ...m, content: m.content + t }));
          },
          toolStart: (e) => setActivity(e.label),
          toolEnd: (e) => setActivity(e.label),
          card: (card) => patch(asstKey, (m) => ({ ...m, cards: [...m.cards, card] })),
          citation: (c) => patch(asstKey, (m) => (m.citations.some((x) => x.id === c.id) ? m : { ...m, citations: [...m.citations, c] })),
          error: (e) => patch(asstKey, (m) => ({ ...m, error: e })),
          done: (d) => {
            finished = true;
            patch(asstKey, (m) => ({ ...m, id: d.message_id ?? undefined, status: d.status, content: d.content, citations: d.citations }));
          },
        },
        ctrl.signal,
      );
      if (!finished) {
        patch(asstKey, (m) => ({ ...m, status: "error", error: m.error ?? { code: "network", message: "The connection dropped before the reply finished." } }));
      }
    } catch (err) {
      if (ctrl.signal.aborted) {
        // Stop: the server saves the partial reply as "stopped" too.
        patch(asstKey, (m) => ({ ...m, status: "stopped" }));
      } else if (err instanceof ApiError && err.status === 429) {
        // Rejected before anything was stored: take the message back and wait.
        setMessages((ms) => ms.filter((m) => m.key !== userKey && m.key !== asstKey));
        setDraft(text);
        setRateUntil(Date.now() + (err.retryAfter ?? 60) * 1000);
        setNow(Date.now());
      } else if (err instanceof ApiError && err.status === 401) {
        router.replace(`/a/${slug}`);
      } else {
        patch(asstKey, (m) => ({ ...m, status: "error", error: { code: "network", message: "Couldn't reach the assistant. Check your connection and try again." } }));
      }
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
      setStreaming(false);
      setActivity(null);
      refreshConversations();
    }
  }

  async function logout() {
    abortRef.current?.abort();
    await api.logout().catch(() => undefined);
    router.replace(`/a/${slug}`);
  }

  const openSource = useCallback((c: Citation, el: HTMLElement | null) => {
    sourceOpener.current = el;
    setSource(c);
  }, []);
  const closeSource = useCallback(() => {
    setSource(null);
    sourceOpener.current?.focus();
  }, []);

  const waitSeconds = rateUntil !== null ? Math.max(1, Math.ceil((rateUntil - now) / 1000)) : 0;
  const lastAssistant = messages.at(-1)?.role === "assistant" ? messages.at(-1) : undefined;

  return (
    <div className="flex h-dvh flex-col bg-paper">
      <header className="flex items-center gap-3 border-b border-rule px-4 py-3 sm:px-6">
        <button
          type="button"
          className="rounded-[var(--radius-control)] border border-rule px-2.5 py-1.5 text-sm md:hidden"
          onClick={() => setListOpen(true)}
          aria-label="Show chats"
        >
          Chats
        </button>
        <OrgSeal emoji={org.branding.logo_emoji} size="sm" />
        <div className="min-w-0 flex-1">
          <p className="truncate font-display text-[17px] leading-tight text-brand">{org.name}</p>
          <p className="truncate text-[13px] text-muted">Chatting with {org.branding.agent_name}</p>
        </div>
        <span className="hidden text-sm text-muted sm:inline">{me.user.display_name}</span>
        <button type="button" onClick={logout} className="rounded-[var(--radius-control)] px-3 py-1.5 text-sm font-medium hover:bg-brand-soft">
          Log out
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <ConversationList
          conversations={conversations}
          activeId={conversationId}
          open={listOpen}
          onClose={() => setListOpen(false)}
          onSelect={(id) => open(id, true)}
          onNew={() => open(null, true)}
        />

        <main className="flex min-w-0 flex-1 flex-col">
          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto" aria-live="polite">
            <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 sm:px-6">
              {load === "loading" && <HistorySkeleton />}
              {load === "error" && (
                <Notice title="Couldn't load this conversation" action={{ label: "Try again", run: () => conversationId && loadMessages(conversationId) }}>
                  Check your connection and try again.
                </Notice>
              )}
              {load === "notfound" && (
                <Notice title="This conversation isn't available" action={{ label: "Start a new chat", run: () => open(null, true) }}>
                  It may have been deleted, or the link is wrong.
                </Notice>
              )}
              {load === "ready" && messages.length === 0 && (
                <section className="flex flex-col gap-5 pt-4 sm:pt-10">
                  <div className="flex items-start gap-3">
                    <OrgSeal emoji={org.branding.logo_emoji} size="md" />
                    <p className="max-w-xl rounded-[var(--radius-bubble)] rounded-tl-none bg-brand-soft px-5 py-4 font-display text-[19px] leading-snug sm:text-[22px]">
                      {org.branding.greeting}
                    </p>
                  </div>
                  <div className="flex flex-col gap-2 sm:pl-14">
                    <p className="text-sm text-muted">You could ask</p>
                    <div className="flex flex-wrap gap-2">
                      {SUGGESTIONS[org.vertical].map((q) => (
                        <button
                          key={q}
                          type="button"
                          onClick={() => send(q)}
                          className="rounded-full border border-rule bg-white px-3.5 py-2 text-left text-sm hover:border-brand hover:text-brand"
                        >
                          {q}
                        </button>
                      ))}
                    </div>
                  </div>
                </section>
              )}
              {load === "ready" &&
                messages.map((m) => (
                  <MessageView
                    key={m.key}
                    message={m}
                    activity={m === lastAssistant && streaming ? activity : null}
                    onOpenSource={openSource}
                  />
                ))}
            </div>
          </div>

          <div className="mx-auto w-full max-w-3xl px-4 pb-4 sm:px-6">
            {rateUntil !== null && (
              <p role="status" data-testid="rate-limit" className="mb-2 rounded-[var(--radius-control)] bg-brand-tint px-3 py-2 text-sm">
                Too many messages — try again in {waitSeconds}s
              </p>
            )}
            <Composer
              value={draft}
              onChange={setDraft}
              onSend={() => send(draft)}
              onStop={() => abortRef.current?.abort()}
              streaming={streaming}
              disabled={rateUntil !== null || load === "loading" || load === "notfound"}
              placeholder={`Message ${org.branding.agent_name}`}
            />
            <p className="mt-1.5 hidden text-[12px] text-muted sm:block">Enter to send, Shift+Enter for a new line</p>
          </div>
        </main>
      </div>

      {source && <SourceDrawer citation={source} onClose={closeSource} />}
    </div>
  );
}

function HistorySkeleton() {
  return (
    <div className="flex flex-col gap-5" aria-label="Loading messages">
      {[60, 85, 45].map((w, i) => (
        <div key={i} className={`flex ${i % 2 ? "" : "justify-end"}`}>
          <div className="pulse-dot h-12 rounded-[var(--radius-bubble)] bg-brand-soft" style={{ width: `${w}%` }} />
        </div>
      ))}
    </div>
  );
}

function Notice({ title, children, action }: { title: string; children: React.ReactNode; action: { label: string; run: () => void } }) {
  return (
    <section className="mt-8 rounded-[var(--radius-bubble)] border border-rule bg-white p-5">
      <h2 className="font-semibold">{title}</h2>
      <p className="mt-1 text-sm text-muted">{children}</p>
      <button type="button" onClick={action.run} className="mt-3 rounded-[var(--radius-control)] bg-brand px-4 py-2 text-sm font-medium text-white">
        {action.label}
      </button>
    </section>
  );
}
