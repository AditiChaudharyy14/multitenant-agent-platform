"use client";

import { useCardEnv } from "@/components/cards/CardEnv";
import { arr, Panel, str } from "@/components/cards/ui";
import type { Card, CardAction } from "@/lib/types";

interface Slot { start: string; display: string; practitioner: string | null; practitioners?: string[]; action: CardAction }

/** "Tue 10 Nov 2026, 14:00" → ["Tue 10 Nov 2026", "14:00"] */
const split = (display: string): [string, string] => {
  const i = display.lastIndexOf(", ");
  return i < 0 ? [display, ""] : [display.slice(0, i), display.slice(i + 2)];
};

/** slot_list and slot_alternatives: free times grouped by day, one chip per start. A chip
 * sends the card's own action, which the server re-validates like a typed message. */
export function SlotList({ card }: { card: Card }) {
  const { busy, send } = useCardEnv();
  const slots = arr<Slot>(card.slots);
  const byDay = new Map<string, Slot[]>();
  for (const s of slots) {
    const [day] = split(s.display);
    byDay.set(day, [...(byDay.get(day) ?? []), s]);
  }
  const alternatives = card.type === "slot_alternatives";
  const title = alternatives ? "Nearest free times" : `Free times for ${str(card.service)}`;
  const meta = [str(card.price_display), typeof card.duration_min === "number" ? `${card.duration_min} min` : "", str(card.practitioner) && `with ${str(card.practitioner)}`]
    .filter(Boolean)
    .join(", ");

  return (
    <Panel title={title} testid="slot-list">
      <div className="px-4 py-3">
        {meta && <p className="text-[13px] text-muted">{meta}</p>}
        {alternatives && str(card.reason) && <p className="mt-1 text-[13px] text-muted">{str(card.reason)}</p>}
        {slots.length === 0 && <p className="mt-1 text-sm text-muted">No free times in that window.</p>}
        {[...byDay].map(([day, list]) => (
          <div key={day} className="mt-3 first:mt-1">
            <p className="text-[14px] font-medium">{day}</p>
            <ul className="mt-1.5 flex flex-wrap gap-2">
              {list.map((s) => {
                const [, time] = split(s.display);
                const who = s.practitioner && (alternatives || !card.practitioner) ? s.practitioner : null;
                return (
                  <li key={s.start + (s.practitioner ?? "")}>
                    <button
                      type="button"
                      data-testid="slot-chip"
                      disabled={busy}
                      onClick={() => send(s.action, `Book ${str(card.service)}, ${s.display}${s.practitioner ? ` with ${s.practitioner}` : ""}`)}
                      className="rounded-full border border-rule bg-white px-3.5 py-1.5 text-sm hover:border-brand hover:text-brand disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-rule disabled:hover:text-ink"
                    >
                      {time}
                      {who && <span className="text-muted"> {who}</span>}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
        {arr<string>(card.notes).map((n) => (
          <p key={n} className="mt-2 text-[13px] text-muted">{n}</p>
        ))}
      </div>
    </Panel>
  );
}
