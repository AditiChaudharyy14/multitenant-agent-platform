"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { CardEnvContext } from "@/components/cards/CardEnv";
import { Composer } from "@/components/chat/Composer";
import { ConversationList } from "@/components/chat/ConversationList";
import { MessageView, type UiMessage } from "@/components/chat/MessageView";
import { MyItemsPanel, PanelDrawer, panelTitle, type PanelData } from "@/components/chat/MyItemsPanel";
import { SourceDrawer } from "@/components/chat/SourceDrawer";
import { OrgSeal } from "@/components/OrgSeal";
import { MAX_MESSAGE_CHARS } from "@/lib/limits";
import { api, ApiError, CHAT_API } from "@/lib/api";
import { streamMessage, type MessageBody } from "@/lib/sse";
import { ADMIN_SUGGESTIONS, SUGGESTIONS } from "@/lib/suggestions";
import type { CardAction, Citation, Conversation, Me, ProposalInfo, StoredMessage } from "@/lib/types";

type Load = "ready" | "loading" | "error" | "notfound";

// Tools that only look things up. Any other tool in a turn may have touched orders or bookings,
// so the side panel reloads after it (and after every button press).
const LOOKUP_ONLY = new Set(["search_knowledge", "search_products", "list_services", "get_opening_hours", "find_available_slots"]);

const key = () => Math.random().toString(36).slice(2);

function fromStored(m: StoredMessage): UiMessage {
  return { key: m.id, id: m.id, role: m.role, content: m.content, status: m.status,
           citations: m.citations ?? [], cards: m.cards ?? [] }; // prettier-ignore
}

/** "customer": the org's assistant. "admin": the read-only analytics chat (admins only; the
 * server enforces that, this only picks the API, the starters and the chrome). */
export type ChatMode = "customer" | "admin";

const ADMIN_GREETING =
  "Ask me how the business is doing. I'm read-only: I can't change anything, and every number comes with its time window and what it counts.";

export function ChatScreen({ me, routeConversationId, mode = "customer" }: { me: Me; routeConversationId: string | null; mode?: ChatMode }) {
  const router = useRouter();
  const { org } = me;
  const slug = org.slug;
  const isAdminChat = mode === "admin";
  const chat = CHAT_API[mode];
  const base = `/a/${slug}/${isAdminChat ? "admin" : "chat"}`;

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
  const [proposals, setProposals] = useState<Record<string, ProposalInfo>>({});
  const [panel, setPanel] = useState<PanelData>({ state: "loading" });
  const [panelVersion, setPanelVersion] = useState(0); // bump to reload the panel
  const [panelOpen, setPanelOpen] = useState(false);
  const sourceOpener = useRef<HTMLElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Refreshes overlap (one after every turn, one after each rename or delete) and responses can
  // arrive out of order. Only the most recently issued request may apply its result, otherwise an
  // older list could bring back a deleted chat or an old title.
  const listSeq = useRef(0);
  const refreshConversations = useCallback(() => {
    const mine = ++listSeq.current;
    chat
      .conversations()
      .then((list) => mine === listSeq.current && setConversations(list))
      .catch(() => undefined);
  }, [chat]);

  useEffect(refreshConversations, [refreshConversations]);

  // My orders / My bookings: one fetch, shown inline on wide screens and in a drawer on narrow ones.
  useEffect(() => {
    if (isAdminChat) return; // the analytics chat has no My orders / My bookings panel
    let live = true;
    const load = org.vertical === "store" ? api.myOrders().then((orders) => ({ orders })) : api.myBookings().then((bookings) => ({ bookings }));
    load
      .then((d) => live && setPanel({ state: "ready", ...d }))
      .catch(() => live && setPanel((p) => ({ ...p, state: "error" })));
    return () => {
      live = false;
    };
  }, [org.vertical, panelVersion, isAdminChat]);

  /** Current status of every proposal in a chat, so each summary card shows the truth. */
  const proposalsSeq = useRef(0); // same rule as the chat list: the latest request wins
  const loadProposals = useCallback(async (id: string) => {
    if (isAdminChat) return; // read-only: no proposals there
    const mine = ++proposalsSeq.current;
    try {
      const list = await api.proposals(id);
      if (convRef.current === id && mine === proposalsSeq.current) setProposals(Object.fromEntries(list.map((p) => [p.proposal_id, p])));
    } catch {
      // cards then show as open; the server still refuses a stale confirm
    }
  }, [isAdminChat]);

  const loadMessages = useCallback(async (id: string) => {
    setLoad("loading");
    setMessages([]);
    try {
      const [stored] = await Promise.all([chat.messages(id), loadProposals(id)]);
      if (convRef.current !== id) return; // the user moved on meanwhile
      setMessages(stored.map(fromStored));
      setLoad("ready");
    } catch (err) {
      if (convRef.current !== id) return;
      setLoad(err instanceof ApiError && err.status === 404 ? "notfound" : "error");
    }
  }, [chat, loadProposals]);

  /** Switch conversation (null = a new one). `push` adds a history entry. */
  const open = useCallback(
    (id: string | null, push: boolean) => {
      abortRef.current?.abort();
      convRef.current = id;
      setConversationId(id);
      setSource(null);
      setProposals({});
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

  /** A typed message. */
  function send(text: string) {
    text = text.trim();
    if (!text) return;
    setDraft("");
    void run({ content: text }, text);
  }

  /** A card button: just another message, with an action the server validates. */
  const sendAction = (action: CardAction, label: string) => void run({ action }, label);

  async function run(body: MessageBody, text: string) {
    if (streaming || rateUntil !== null) return;
    let touchedItems = "action" in body; // a button press always may change something
    const userKey = key();
    const asstKey = key();
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
        id = (await chat.createConversation()).id;
        convRef.current = id;
        setConversationId(id);
        // Same page component for /chat and /chat/<id>: the URL changes, the screen stays.
        window.history.replaceState(null, "", `${base}/${id}`);
      }
      await streamMessage(
        id,
        body,
        {
          token: (t) => {
            setActivity(null);
            patch(asstKey, (m) => ({ ...m, content: m.content + t }));
          },
          toolStart: (e) => setActivity(e.label),
          toolEnd: (e) => {
            setActivity(e.label);
            if (!LOOKUP_ONLY.has(e.name)) touchedItems = true;
          },
          card: (card) => patch(asstKey, (m) => ({ ...m, cards: [...m.cards, card] })),
          citation: (c) => patch(asstKey, (m) => (m.citations.some((x) => x.id === c.id) ? m : { ...m, citations: [...m.citations, c] })),
          error: (e) => patch(asstKey, (m) => ({ ...m, error: e })),
          done: (d) => {
            finished = true;
            patch(asstKey, (m) => ({ ...m, id: d.message_id ?? undefined, status: d.status, content: d.content, citations: d.citations }));
          },
        },
        ctrl.signal,
        chat.base,
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
        if ("content" in body) setDraft(text);
        setRateUntil(Date.now() + (err.retryAfter ?? 60) * 1000);
        setNow(Date.now());
      } else if (err instanceof ApiError && err.status === 409) {
        // Another reply in this conversation is still being written (e.g. in a second tab).
        if ("content" in body) setDraft(text);
        patch(asstKey, (m) => ({ ...m, status: "error", error: { code: "busy", message: "A reply is still in progress. Wait for it to finish, then send again." } }));
      } else if (err instanceof ApiError && err.status === 422) {
        // Refused by the server's input limits (e.g. too long): nothing was stored.
        if ("content" in body) setDraft(text);
        patch(asstKey, (m) => ({ ...m, status: "error", error: { code: "invalid", message: `Messages can be at most ${MAX_MESSAGE_CHARS.toLocaleString("en-US")} characters. Shorten it and try again.` } }));
      } else if (err instanceof ApiError && err.status === 401) {
        // The session ended mid-chat (expired or logged out elsewhere): back to login, saying why.
        router.replace(`/a/${slug}?reason=expired`);
      } else {
        patch(asstKey, (m) => ({ ...m, status: "error", error: { code: "network", message: "Couldn't reach the assistant. Check your connection and try again." } }));
      }
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
      setStreaming(false);
      setActivity(null);
      refreshConversations();
      if (convRef.current) void loadProposals(convRef.current); // older summaries may now be superseded
      if (touchedItems) setPanelVersion((v) => v + 1);
    }
  }

  async function renameConversation(id: string, title: string) {
    await chat.renameConversation(id, title).catch(() => undefined);
    refreshConversations();
  }

  async function deleteConversation(id: string) {
    await chat.deleteConversation(id).catch(() => undefined);
    refreshConversations();
    if (convRef.current === id) open(null, true);
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
      <header className="flex items-center gap-2 border-b border-rule px-3 py-3 sm:gap-3 sm:px-6">
        <button
          type="button"
          className="shrink-0 rounded-[var(--radius-control)] border border-rule px-2.5 py-1.5 text-sm min-[769px]:hidden"
          onClick={() => setListOpen(true)}
        >
          Chats
        </button>
        <span className="max-[420px]:hidden"><OrgSeal emoji={org.branding.logo_emoji} size="sm" /></span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-display text-[17px] leading-tight text-brand">{org.name}</p>
          <p className="hidden truncate text-[13px] text-muted sm:block">
            {isAdminChat ? "Analytics · read-only" : `Chatting with ${org.branding.agent_name}`}
          </p>
        </div>
        {/* Shown only to admins; the admin routes themselves answer 403 to anyone else. */}
        {me.user.role === "admin" && (
          <Link
            href={isAdminChat ? `/a/${slug}/chat` : `/a/${slug}/admin`}
            data-testid={isAdminChat ? "customer-chat-link" : "analytics-link"}
            className="shrink-0 rounded-[var(--radius-control)] border border-rule px-2.5 py-1.5 text-sm font-medium hover:border-brand hover:text-brand"
          >
            {isAdminChat ? "Customer chat" : "Analytics"}
          </Link>
        )}
        {!isAdminChat && (
          <button
            type="button"
            className="shrink-0 rounded-[var(--radius-control)] border border-rule px-2.5 py-1.5 text-sm min-[1100px]:hidden"
            onClick={() => setPanelOpen(true)}
          >
            {panelTitle(org.vertical)}
          </button>
        )}
      </header>

      <div className="flex min-h-0 flex-1">
        <ConversationList
          conversations={conversations}
          activeId={conversationId}
          open={listOpen}
          onClose={() => setListOpen(false)}
          onSelect={(id) => open(id, true)}
          onNew={() => open(null, true)}
          onRename={renameConversation}
          onDelete={deleteConversation}
          onLogout={logout}
          userName={me.user.display_name}
        />

        <CardEnvContext.Provider value={{ busy: streaming || rateUntil !== null, send: sendAction, proposals }}>
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
                      {isAdminChat ? ADMIN_GREETING : org.branding.greeting}
                    </p>
                  </div>
                  <div className="flex flex-col gap-2 sm:pl-14">
                    <p className="text-sm text-muted">You could ask</p>
                    <div className="flex flex-wrap gap-2">
                      {(isAdminChat ? ADMIN_SUGGESTIONS : SUGGESTIONS)[org.vertical].map((q) => (
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
              placeholder={isAdminChat ? "Ask about your numbers" : `Message ${org.branding.agent_name}`}
            />
            <p className="mt-1.5 hidden text-[12px] text-muted sm:block">Enter to send, Shift+Enter for a new line</p>
          </div>
        </main>
        </CardEnvContext.Provider>

        {!isAdminChat && (
          <aside className="hidden w-80 shrink-0 border-l border-rule min-[1100px]:block">
            <MyItemsPanel vertical={org.vertical} data={panel} onRetry={() => setPanelVersion((v) => v + 1)} onAsk={send}
              memory={(org.features?.memory ?? false) && me.user.role === "customer"} version={panelVersion} />
          </aside>
        )}
      </div>

      {panelOpen && (
        <PanelDrawer label={panelTitle(org.vertical)} onClose={() => setPanelOpen(false)}>
          <MyItemsPanel
            vertical={org.vertical}
            data={panel}
            memory={(org.features?.memory ?? false) && me.user.role === "customer"}
            version={panelVersion}
            onRetry={() => setPanelVersion((v) => v + 1)}
            onAsk={(t) => {
              setPanelOpen(false);
              send(t);
            }}
          />
        </PanelDrawer>
      )}

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
