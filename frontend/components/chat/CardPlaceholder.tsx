import type { Card } from "@/lib/types";

// Interactive cards (proposals, slot pickers) arrive in part 2. For now, show that one is there.
export function CardPlaceholder({ card }: { card: Card }) {
  return (
    <div data-testid="card-placeholder" className="mt-3 rounded-[var(--radius-control)] border border-dashed border-brand/40 bg-brand-soft px-4 py-3 text-sm">
      <span className="font-medium text-brand">Card:</span> <code>{card.type}</code>
    </div>
  );
}
