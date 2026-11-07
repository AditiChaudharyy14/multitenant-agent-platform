"use client";

import { useEffect } from "react";

import { BookingRows, OrderRows } from "@/components/cards/ReadCards";
import type { MyBookings, MyOrders, Vertical } from "@/lib/types";

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
}

export const panelTitle = (v: Vertical) => (v === "store" ? "My orders" : "My bookings");

/** The customer's own orders or bookings, from the same domain functions the agent's tools use. */
export function MyItemsPanel({ vertical, data, onRetry, onAsk }: Props) {
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
      </div>
    </section>
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
