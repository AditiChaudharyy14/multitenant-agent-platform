"use client";

import { useState } from "react";

import { useCardEnv } from "@/components/cards/CardEnv";
import { arr, str } from "@/components/cards/ui";
import type { Card, ProposalState } from "@/lib/types";

interface Line { sku: string; name: string; qty: number; line_total_display: string }
interface Diff { sku: string; name: string; old_qty: number; new_qty: number }

const TITLES: Record<string, string> = {
  create_order: "Your order", reorder: "Your order", update_order: "Changes to your order", cancel_order: "Cancel this order",
  create_booking: "Your booking", reschedule_booking: "Move your booking", cancel_booking: "Cancel this booking",
};

const NOTES: Record<Exclude<ProposalState, "open" | "confirmed">, string> = {
  superseded: "Replaced by a newer summary below.",
  expired: "This summary expired. Ask again and I'll prepare a fresh one.",
  rejected: "This couldn't be completed. Ask again and I'll check.",
};

/** order_summary and booking_summary: the "slip" the customer confirms. Its state comes from
 * the server (open / confirmed / superseded / expired), so a card loaded from history is as
 * honest as one that just streamed in. */
export function ProposalCard({ card }: { card: Card }) {
  const { busy, send, proposals } = useCardEnv();
  const [pressed, setPressed] = useState(false);
  const id = str(card.proposal_id);
  const kind = str(card.kind);
  const info = proposals[id];
  const state: ProposalState = info?.status === "open" && Date.parse(info.expires_at) <= Date.now() ? "expired" : (info?.status ?? "open");
  const label = str(arr<{ label?: string }>(card.actions)[0]?.label) || "Confirm";
  const dead = state !== "open" && state !== "confirmed";
  const isBooking = card.type === "booking_summary";

  return (
    <article
      data-testid="proposal-card"
      data-state={state}
      data-kind={kind}
      aria-label={TITLES[kind] ?? "Summary"}
      className={`relative mt-3 min-w-0 max-w-full rounded-[var(--radius-control)] border bg-white ${dead ? "border-rule opacity-60" : "border-brand/30"}`}
    >
      <div className="px-4 pb-3 pt-3.5">
        <h3 className="font-display text-[18px] leading-snug text-brand">{TITLES[kind] ?? "Summary"}</h3>
        {str(card.order_number) || str(card.booking_number) ? (
          <p className="text-[13px] text-muted">{str(card.order_number) || str(card.booking_number)}</p>
        ) : null}
        {isBooking ? <BookingBody card={card} /> : <OrderBody card={card} />}
        {arr<string>(card.changes).map((c) => (
          <p key={c} className="mt-2 rounded-[var(--radius-control)] bg-brand-tint px-3 py-2 text-[13px]">{c}</p>
        ))}
      </div>

      {/* The tear line: the card's one flourish. */}
      <div className="relative" aria-hidden>
        <span className="absolute -left-[7px] -top-[7px] h-3.5 w-3.5 rounded-full border border-rule bg-paper" />
        <span className="absolute -right-[7px] -top-[7px] h-3.5 w-3.5 rounded-full border border-rule bg-paper" />
        <div className="mx-3 border-t border-dashed border-rule" />
      </div>

      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3">
        <p className="min-w-0 text-[15px]">
          <span className="text-muted">{isBooking ? (kind === "cancel_booking" ? "Fee " : "Price ") : "Total "}</span>
          <strong data-testid="proposal-total" className="font-semibold text-brand">
            {isBooking ? str(card.fee_display) || str(card.price_display) : str(card.total_display)}
          </strong>
        </p>
        {state === "confirmed" ? (
          <p data-testid="confirmed-tag" className="text-[15px] font-semibold text-brand">Confirmed ✓</p>
        ) : state === "open" ? (
          <button
            type="button"
            disabled={busy || pressed}
            onClick={() => {
              setPressed(true);
              send({ type: "confirm_proposal", proposal_id: id }, "Confirm");
            }}
            className="rounded-[var(--radius-control)] bg-brand px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {label}
          </button>
        ) : (
          <p data-testid="proposal-note" className="text-[13px] text-muted">{NOTES[state as keyof typeof NOTES]}</p>
        )}
      </div>
      {state === "open" && str(card.expires_at) && (
        <p className="px-4 pb-3 text-[12px] text-muted">
          {isBooking ? "Paid at the studio. " : "Cash on delivery. "}Open until {str(card.expires_at)}.
        </p>
      )}
    </article>
  );
}

function OrderBody({ card }: { card: Card }) {
  const lines = arr<Line>(card.lines);
  const diff = arr<Diff>(card.diff);
  return (
    <>
      <ul className="mt-2 divide-y divide-rule text-[14px]">
        {lines.map((l) => (
          <li key={l.sku} className="flex items-baseline justify-between gap-3 py-1.5">
            <span className="min-w-0 [overflow-wrap:anywhere]">
              {l.qty} × {l.name}
            </span>
            <span className="shrink-0 text-muted">{l.line_total_display}</span>
          </li>
        ))}
      </ul>
      {diff.length > 0 && (
        <p className="mt-1 text-[13px] text-muted">
          {diff.map((d) => `${d.name}: ${d.old_qty} → ${d.new_qty}`).join(", ")}
        </p>
      )}
      {str(card.delivery_address) && (
        <p className="mt-2 text-[13px] text-muted [overflow-wrap:anywhere]">
          Deliver to {str(card.delivery_address)}
          {str(card.old_delivery_address) && str(card.old_delivery_address) !== str(card.delivery_address) ? ` (was ${str(card.old_delivery_address)})` : ""}
        </p>
      )}
    </>
  );
}

function BookingBody({ card }: { card: Card }) {
  const old = card.old as { when?: string; practitioner?: string } | undefined;
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[14px]">
      <dt className="text-muted">Service</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]">{str(card.service)}</dd>
      <dt className="text-muted">With</dt>
      <dd>
        {str(card.practitioner)}
        {card.practitioner_auto_assigned ? <span className="text-muted"> (assigned)</span> : null}
      </dd>
      <dt className="text-muted">Date</dt>
      <dd>{str(card.date) || str(card.when).split(",")[0]}</dd>
      <dt className="text-muted">Time</dt>
      <dd>
        {str(card.time) || str(card.when).split(", ").pop()}
        {typeof card.duration_min === "number" ? <span className="text-muted">, {card.duration_min} min</span> : null}
      </dd>
      {old?.when && (
        <>
          <dt className="text-muted">Was</dt>
          <dd className="text-muted">{old.when} with {old.practitioner}</dd>
        </>
      )}
      {str(card.fee_rule) && (
        <>
          <dt className="text-muted">Fee rule</dt>
          <dd className="text-muted">{str(card.fee_rule)}</dd>
        </>
      )}
    </dl>
  );
}
