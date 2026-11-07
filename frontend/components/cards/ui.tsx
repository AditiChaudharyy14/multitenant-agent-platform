import type { ReactNode } from "react";

/** The status words the backend uses for orders and bookings, in the customer's words. */
const TONES: Record<string, { label: string; cls: string }> = {
  pending: { label: "Pending", cls: "bg-brand-tint text-ink" },
  confirmed: { label: "Confirmed", cls: "bg-brand-soft text-brand" },
  shipped: { label: "On its way", cls: "bg-brand-soft text-brand" },
  delivered: { label: "Delivered", cls: "bg-brand text-white" },
  cancelled: { label: "Cancelled", cls: "bg-rule text-muted" },
  completed: { label: "Completed", cls: "bg-rule text-muted" },
  past: { label: "Past", cls: "bg-rule text-muted" },
  in_stock: { label: "In stock", cls: "bg-brand-soft text-brand" },
  low: { label: "Low stock", cls: "bg-brand-tint text-ink" },
  out: { label: "Out of stock", cls: "bg-rule text-muted" },
};

export function StatusBadge({ status }: { status: string }) {
  const t = TONES[status] ?? { label: status.charAt(0).toUpperCase() + status.slice(1), cls: "bg-rule text-muted" };
  return (
    <span data-testid="status-badge" data-status={status} className={`inline-block shrink-0 whitespace-nowrap rounded-full px-2.5 py-0.5 text-[12px] font-medium [word-spacing:0.12em] ${t.cls}`}>
      {t.label}
    </span>
  );
}

/** A plain bordered card for the read-only results. */
export function Panel({ title, children, testid }: { title?: string; children: ReactNode; testid: string }) {
  return (
    <section data-testid={testid} className="mt-3 min-w-0 max-w-full rounded-[var(--radius-control)] border border-rule bg-white">
      {title && <h3 className="border-b border-rule px-4 py-2.5 font-display text-[16px] leading-snug">{title}</h3>}
      {children}
    </section>
  );
}

export const str = (v: unknown): string => (typeof v === "string" ? v : "");
export const arr = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
