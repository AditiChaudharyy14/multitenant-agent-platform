"use client";

import { useCallback, useEffect, useState } from "react";

import { BookingRows, OrderRows } from "@/components/cards/ReadCards";
import { api } from "@/lib/api";
import type { Memory, MyBookings, MyOrders, Vertical } from "@/lib/types";

export interface PanelData {
  state: "loading" | "ready" | "error";
  orders?: MyOrders;
  bookings?: MyBookings;
}

interface Props {
  vertical: Vertical;
  data: PanelData;
  onRetry: () => void;
  onAsk: (text: string) => void;
  memory?: boolean; // org.features.memory: show "What I remember"
  version?: number; // bumped after a turn that may have changed something
}

export const panelTitle = (v: Vertical) => (v === "store" ? "My orders" : "My bookings");

/** The customer's own orders or bookings, from the same domain functions the agent's tools use. */
export function MyItemsPanel({ vertical, data, onRetry, onAsk, memory = false, version = 0 }: Props) {
  const empty = vertical === "store" ? (data.orders?.orders.length ?? 0) === 0 : (data.bookings?.upcoming.length ?? 0) + (data.bookings?.past.length ?? 0) === 0;
  return (
    <section data-testid="my-items" aria-label={panelTitle(vertical)} className="flex h-full min-h-0 flex-col">
      <h2 className="border-b border-rule px-4 py-3 font-display text-[18px]">{panelTitle(vertical)}</h2>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {data.state === "error" && (
          <div className="px-4 py-4 text-sm">
            <p className="text-muted">Couldn't load this list.</p>
            <button type="button" onClick={onRetry} className="mt-2 rounded-[var(--radius-control)] border border-rule px-3 py-1.5 font-medium">Try again</button>
          </div>
        )}
        {data.state === "loading" && !data.orders && !data.bookings && <p className="px-4 py-4 text-sm text-muted">Loading…</p>}
        {data.state !== "error" && (data.orders || data.bookings) && empty && (
          <div className="px-4 py-4 text-sm">
            <p className="text-muted">{vertical === "store" ? "No orders yet." : "No bookings yet."}</p>
            <button
              type="button"
              onClick={() => onAsk(vertical === "store" ? "What teas do you have?" : "What times are free this week?")}
              className="mt-2 rounded-[var(--radius-control)] border border-rule px-3 py-1.5 font-medium hover:border-brand"
            >
              {vertical === "store" ? "Browse the teas" : "See free times"}
            </button>
          </div>
        )}
        {vertical === "store" && data.orders && !empty && <OrderRows orders={data.orders.orders} />}
        {vertical === "booking" && data.bookings && !empty && (
          <>
            {data.bookings.upcoming.length > 0 && (
              <>
                <p className="px-4 pt-3 text-[13px] font-medium text-muted">Upcoming</p>
                <BookingRows bookings={data.bookings.upcoming} />
              </>
            )}
            {data.bookings.past.length > 0 && (
              <>
                <p className="border-t border-rule px-4 pt-3 text-[13px] font-medium text-muted">Past and cancelled</p>
                <BookingRows bookings={data.bookings.past} />
              </>
            )}
          </>
        )}
        {memory && <RememberedSection version={version} />}
      </div>
    </section>
  );
}

const MEMORY_LABELS: Record<string, string> = {
  usual_products: "Your usual",
  usual_practitioner: "Usual practitioner",
  usual_service: "Usual service",
};
const memoryLabel = (key: string) => MEMORY_LABELS[key] ?? (key.charAt(0).toUpperCase() + key.slice(1)).replace(/_/g, " ");

/** "What I remember": the same list the agent's get_my_preferences returns, each with a
 * delete button. Derived facts ("your usual") come from the customer's own history;
 * deleting one hides it for good. */
function RememberedSection({ version }: { version: number }) {
  const [items, setItems] = useState<Memory[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api.memories()
      .then((r) => { setItems(r.memories); setFailed(false); })
      .catch(() => setFailed(true));
  }, []);
  useEffect(load, [load, version]);

  async function forget(key: string) {
    setBusy(key);
    try {
      await api.forgetMemory(key);
      setItems((xs) => (xs ?? []).filter((m) => m.key !== key));
    } catch {
      load(); // already gone (404) or a network error: show what the server has
    } finally {
      setBusy(null);
    }
  }

  return (
    <div data-testid="memories" className="border-t border-rule px-4 py-3">
      <h3 className="text-[13px] font-medium text-muted">What I remember</h3>
      {failed && <p className="mt-1 text-sm text-muted">Couldn't load this.</p>}
      {items && items.length === 0 && (
        <p className="mt-1 text-sm text-muted">Nothing yet. Ask me to remember a preference, like a favourite practitioner or how you take your tea.</p>
      )}
      {items && items.length > 0 && (
        <ul className="mt-1.5 flex flex-col gap-2">
          {items.map((m) => (
            <li key={m.key} data-testid="memory-row" className="flex items-start gap-2 text-sm">
              <div className="min-w-0 flex-1">
                <p className="font-medium text-ink">
                  {memoryLabel(m.key)}
                  <span className="ml-1.5 text-[12px] font-normal text-muted">{m.source === "derived" ? "from your history" : "you told me"}</span>
                </p>
                <p className="text-muted [overflow-wrap:anywhere]">{m.value}</p>
              </div>
              <button
                type="button"
                onClick={() => forget(m.key)}
                disabled={busy === m.key}
                aria-label={`Forget ${memoryLabel(m.key)}`}
                className="shrink-0 rounded-[var(--radius-control)] border border-rule px-2 py-0.5 text-[12px] text-muted hover:border-brand hover:text-ink disabled:opacity-50"
              >
                Forget
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The panel as a drawer from the right, below 1100px. */
export function PanelDrawer({ label, onClose, children }: { label: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-30 min-[1100px]:hidden">
      <div className="absolute inset-0 bg-ink/25" onClick={onClose} aria-hidden />
      <div role="dialog" aria-modal="true" aria-label={label} className="drawer-in absolute inset-y-0 right-0 w-[90%] max-w-sm bg-paper shadow-xl">
        <button type="button" onClick={onClose} aria-label={`Close ${label}`} className="absolute right-2 top-2.5 rounded-[var(--radius-control)] px-2 py-1 text-xl leading-none text-muted hover:bg-brand-soft hover:text-ink">
          ×
        </button>
        {children}
      </div>
    </div>
  );
}
