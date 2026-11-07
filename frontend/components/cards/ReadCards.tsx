import { arr, Panel, StatusBadge, str } from "@/components/cards/ui";
import type { BookingRow, Card, OrderRow } from "@/lib/types";

// ───────────── order_status ─────────────

interface Step { from_status: string | null; to_status: string; at: string }
interface Line { sku: string; name: string; qty: number; line_total_display: string }

const STEP_WORDS: Record<string, string> = {
  pending: "Order placed", confirmed: "Confirmed by the shop", shipped: "On its way", delivered: "Delivered", cancelled: "Cancelled",
};

export function OrderStatus({ card }: { card: Card }) {
  const steps = arr<Step>(card.history);
  return (
    <Panel title={`Order ${str(card.order_number)}`} testid="order-status">
      <div className="px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <StatusBadge status={str(card.status)} />
          <p className="text-[15px] font-semibold text-brand">{str(card.total_display)}</p>
        </div>
        <ol className="mt-3 border-l border-rule pl-4" aria-label="Status history">
          {steps.map((s, i) => (
            <li key={i} className="relative pb-3 last:pb-0">
              <span aria-hidden className={`absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ${i === steps.length - 1 ? "bg-brand" : "bg-rule"}`} />
              <p className="text-[14px] font-medium">{STEP_WORDS[s.to_status] ?? s.to_status}</p>
              <p className="text-[13px] text-muted">{s.at}</p>
            </li>
          ))}
        </ol>
        <ul className="mt-3 divide-y divide-rule border-t border-rule text-[14px]">
          {arr<Line>(card.items).map((l) => (
            <li key={l.sku} className="flex justify-between gap-3 py-1.5">
              <span className="min-w-0 [overflow-wrap:anywhere]">{l.qty} × {l.name}</span>
              <span className="shrink-0 text-muted">{l.line_total_display}</span>
            </li>
          ))}
        </ul>
        {str(card.delivery_address) && <p className="mt-2 text-[13px] text-muted [overflow-wrap:anywhere]">Deliver to {str(card.delivery_address)}. {str(card.payment_method)}.</p>}
        {card.can_edit ? <p className="mt-1 text-[13px] text-muted">You can still change or cancel this order.</p> : null}
      </div>
    </Panel>
  );
}

// ───────────── lists: shared rows, also used by the side panel ─────────────

export function OrderRows({ orders }: { orders: OrderRow[] }) {
  return (
    <ul className="divide-y divide-rule">
      {orders.map((o) => (
        <li key={o.order_number} data-testid="order-row" className="flex min-w-0 items-center justify-between gap-3 px-4 py-2.5">
          <div className="min-w-0">
            <p className="text-[14px] font-medium">{o.order_number}</p>
            <p className="truncate text-[13px] text-muted">{o.placed_at}, {o.units} {o.units === 1 ? "item" : "items"}</p>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <StatusBadge status={o.status} />
            <p className="text-[13px] text-muted">{o.total_display}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}

export function BookingRows({ bookings }: { bookings: BookingRow[] }) {
  return (
    <ul className="divide-y divide-rule">
      {bookings.map((b) => (
        <li key={b.booking_number} data-testid="booking-row" className="flex min-w-0 items-center justify-between gap-3 px-4 py-2.5">
          <div className="min-w-0">
            <p className="truncate text-[14px] font-medium">{b.service}</p>
            <p className="truncate text-[13px] text-muted">{b.when} with {b.practitioner}</p>
            <p className="text-[12px] text-muted">{b.booking_number}</p>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <StatusBadge status={b.status} />
            <p className="text-[13px] text-muted">
              {b.status === "cancelled" && b.cancellation_fee_display ? `Fee ${b.cancellation_fee_display}` : b.price_display}
            </p>
          </div>
        </li>
      ))}
    </ul>
  );
}

export function OrderList({ card }: { card: Card }) {
  const orders = arr<OrderRow>(card.orders);
  return (
    <Panel title="Your orders" testid="order-list">
      {orders.length ? <OrderRows orders={orders} /> : <p className="px-4 py-3 text-sm text-muted">No orders yet.</p>}
    </Panel>
  );
}

export function BookingList({ card }: { card: Card }) {
  const upcoming = arr<BookingRow>(card.upcoming);
  const past = arr<BookingRow>(card.past);
  return (
    <Panel title="Your bookings" testid="booking-list">
      {upcoming.length + past.length === 0 && <p className="px-4 py-3 text-sm text-muted">No bookings yet.</p>}
      {upcoming.length > 0 && (
        <>
          <p className="px-4 pt-2.5 text-[13px] font-medium text-muted">Upcoming</p>
          <BookingRows bookings={upcoming} />
        </>
      )}
      {past.length > 0 && (
        <>
          <p className="border-t border-rule px-4 pt-2.5 text-[13px] font-medium text-muted">Past and cancelled</p>
          <BookingRows bookings={past} />
        </>
      )}
    </Panel>
  );
}

// ───────────── product_list ─────────────

interface Product { sku: string; name: string; category: string; price_display: string; stock: number }
const LOW_STOCK = 5;

export function ProductList({ card }: { card: Card }) {
  const products = arr<Product>(card.products);
  return (
    <Panel title="Tea on the shelf" testid="product-list">
      {products.length === 0 && <p className="px-4 py-3 text-sm text-muted">Nothing matched that search.</p>}
      <ul className="divide-y divide-rule">
        {products.map((p) => (
          <li key={p.sku} className="flex min-w-0 items-center justify-between gap-3 px-4 py-2.5">
            <div className="min-w-0">
              <p className="text-[14px] font-medium [overflow-wrap:anywhere]">{p.name}</p>
              <p className="text-[13px] text-muted">{p.category}</p>
            </div>
            <div className="flex shrink-0 flex-col items-end gap-1">
              <p className="text-[14px] font-semibold text-brand">{p.price_display}</p>
              <StatusBadge status={p.stock <= 0 ? "out" : p.stock <= LOW_STOCK ? "low" : "in_stock"} />
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

// ───────────── outcome cards (after Confirm) and small extras ─────────────

const OUTCOMES: Record<string, string> = {
  order_confirmed: "Order placed", order_updated: "Order updated", order_cancelled: "Order cancelled",
  booking_confirmed: "Booking made", booking_rescheduled: "Booking moved", booking_cancelled: "Booking cancelled",
};

export function Outcome({ card }: { card: Card }) {
  const number = str(card.order_number) || str(card.booking_number);
  const old = card.old as { when?: string } | undefined;
  const detail = [
    str(card.service) && `${str(card.service)}${str(card.practitioner) ? ` with ${str(card.practitioner)}` : ""}`,
    str(card.when) && (old?.when ? `${str(card.when)} (was ${old.when})` : str(card.when)),
    str(card.total_display) && `Total ${str(card.total_display)}`,
    str(card.fee_display) && Number(card.fee) > 0 ? `Fee ${str(card.fee_display)}` : "",
  ].filter(Boolean);
  return (
    <Panel testid="outcome-card">
      <div className="flex items-start justify-between gap-3 px-4 py-3">
        <div className="min-w-0">
          <p className="font-display text-[16px]">{OUTCOMES[card.type] ?? "Done"}</p>
          <p className="text-[13px] text-muted">{number}</p>
          {detail.map((d) => (
            <p key={d} className="text-[13px] [overflow-wrap:anywhere]">{d}</p>
          ))}
        </div>
        {str(card.status) && <StatusBadge status={str(card.status)} />}
      </div>
    </Panel>
  );
}

interface ShortLine { sku: string; name: string; requested: number; available: number; alternatives: Product[] }

export function StockIssue({ card }: { card: Card }) {
  return (
    <Panel title="Not enough stock" testid="stock-issue">
      <ul className="divide-y divide-rule">
        {arr<ShortLine>(card.lines).map((l) => (
          <li key={l.sku} className="px-4 py-2.5 text-[14px]">
            <p>{l.name}: you asked for {l.requested}, {l.available} available.</p>
            {l.alternatives.length > 0 && (
              <p className="text-[13px] text-muted">Instead: {l.alternatives.map((a) => `${a.name} (${a.price_display})`).join(", ")}</p>
            )}
          </li>
        ))}
      </ul>
    </Panel>
  );
}

interface Svc { name: string; duration_min: number; price_display: string; practitioners: string[] }

export function ServiceList({ card }: { card: Card }) {
  return (
    <Panel title="Services" testid="service-list">
      <ul className="divide-y divide-rule">
        {arr<Svc>(card.services).map((s) => (
          <li key={s.name} className="flex min-w-0 items-center justify-between gap-3 px-4 py-2.5">
            <div className="min-w-0">
              <p className="text-[14px] font-medium">{s.name}</p>
              <p className="text-[13px] text-muted">{s.duration_min} min, with {s.practitioners.join(", ")}</p>
            </div>
            <p className="shrink-0 text-[14px] font-semibold text-brand">{s.price_display}</p>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

interface Day { day: string; closed: boolean; opens: string | null; closes: string | null }

export function OpeningHours({ card }: { card: Card }) {
  const holidays = arr<{ date: string; name: string }>(card.holidays);
  return (
    <Panel title="Opening hours" testid="opening-hours">
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 px-4 py-3 text-[14px]">
        {arr<Day>(card.week).map((d) => (
          <div key={d.day} className="contents">
            <dt className="text-muted">{d.day}</dt>
            <dd>{d.closed ? "Closed" : `${d.opens} to ${d.closes}`}</dd>
          </div>
        ))}
      </dl>
      {holidays.length > 0 && <p className="border-t border-rule px-4 py-2.5 text-[13px] text-muted">Closed on {holidays.map((h) => `${h.date} (${h.name})`).join(", ")}.</p>}
    </Panel>
  );
}
