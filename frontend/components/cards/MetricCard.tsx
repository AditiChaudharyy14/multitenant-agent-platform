import { Panel } from "@/components/cards/ui";
import type { Card } from "@/lib/types";

// The admin analytics agent's metric card (backend/app/domain/analytics/metrics.py):
// { metric, title, values, window: {label, timezone, ...}, definition }. Rendered from the
// shape of `values`, so a new metric needs no new component: headline numbers become stat
// tiles, a {key: count} map a row of small tiles, and a list of rows a table. The window and
// definition are always shown as the footnote, so a number is never shown without them.

type Row = Record<string, unknown>;
interface Money { amount: string; display: string }

const LABELS: Record<string, string> = {
  pct: "%", utilisation_pct: "Utilisation", conversion_rate_pct: "Conversion rate", sku: "SKU",
  estimated_cost_usd: "Est. cost (USD)", stock_now: "In stock now", input_tokens: "Input tokens", output_tokens: "Output tokens",
};

const label = (key: string) => LABELS[key] ?? (key.charAt(0).toUpperCase() + key.slice(1)).replace(/_/g, " ");
const isMoney = (v: unknown): v is Money => typeof v === "object" && v !== null && "display" in v && "amount" in v;
const isRows = (v: unknown): v is Row[] => Array.isArray(v) && v.every((r) => typeof r === "object" && r !== null);
const isCounts = (v: unknown): v is Record<string, number> =>
  typeof v === "object" && v !== null && !Array.isArray(v) && Object.values(v).every((x) => typeof x === "number");

function show(key: string, v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (isMoney(v)) return v.display;
  if (key.endsWith("_pct")) return `${v}%`;
  if (typeof v === "number") return v.toLocaleString("en-US");
  return String(v);
}

// Keys that are context, not headline numbers.
const QUIET = new Set(["pricing_note", "unpriced_models", "note"]);

export function MetricCard({ card }: { card: Card }) {
  const values = (card.values ?? {}) as Row;
  const window = (card.window ?? {}) as { label?: string; timezone?: string };
  const entries = Object.entries(values).filter(([k]) => !QUIET.has(k));
  const tiles = entries.filter(([, v]) => !isRows(v) && !isCounts(v));
  const counts = entries.filter(([, v]) => isCounts(v)) as [string, Record<string, number>][];
  const tables = entries.filter(([, v]) => isRows(v)) as [string, Row[]][];
  const unpriced = Array.isArray(values.unpriced_models) ? (values.unpriced_models as string[]) : [];

  return (
    <Panel title={String(card.title ?? "Metric")} testid="metric-card">
      <div className="flex flex-col gap-3 px-4 py-3">
        {tiles.length > 0 && (
          <dl className="flex flex-wrap gap-x-6 gap-y-2">
            {tiles.map(([k, v]) => (
              <div key={k} data-testid="metric-value">
                <dt className="text-[12px] text-muted">{label(k)}</dt>
                <dd className="font-display text-[22px] leading-tight text-ink">{show(k, v)}</dd>
              </div>
            ))}
          </dl>
        )}
        {counts.map(([k, map]) => (
          <div key={k}>
            <p className="text-[12px] text-muted">{label(k)}</p>
            <ul className="mt-1 flex flex-wrap gap-2">
              {Object.entries(map).map(([name, n]) => (
                <li key={name} className="rounded-[var(--radius-control)] border border-rule px-2.5 py-1 text-[13px]">
                  <span className="text-muted">{label(name)}</span> <strong className="font-semibold text-ink">{n}</strong>
                </li>
              ))}
            </ul>
          </div>
        ))}
        {tables.map(([k, rows]) => <MetricTable key={k} rows={rows} />)}
      </div>
      <footer data-testid="metric-footnote" className="border-t border-rule px-4 py-2.5 text-[12px] leading-snug text-muted">
        <p>
          <span className="font-medium text-ink">{window.label}</span>
          {window.timezone ? ` (${window.timezone})` : ""}
        </p>
        <p className="mt-0.5">{String(card.definition ?? "")}</p>
        {typeof values.note === "string" && <p className="mt-0.5 text-ink">{values.note}</p>}
        {typeof values.pricing_note === "string" && <p className="mt-0.5">Cost {values.pricing_note}.</p>}
        {unpriced.length > 0 && <p className="mt-0.5">No price on file for: {unpriced.join(", ")}.</p>}
      </footer>
    </Panel>
  );
}

function MetricTable({ rows }: { rows: Row[] }) {
  if (rows.length === 0) return <p className="text-[13px] text-muted">Nothing in this window.</p>;
  const cols = Object.keys(rows[0]);
  const pctCol = cols.find((c) => c.endsWith("_pct"));
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="w-full min-w-[320px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-rule text-left text-muted">
            {cols.map((c) => (
              <th key={c} scope="col" className="px-4 py-1.5 font-medium">{label(c)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-rule last:border-0">
              {cols.map((c) => (
                <td key={c} className="px-4 py-1.5 align-middle [overflow-wrap:anywhere]">
                  {c === pctCol && r[c] !== null ? <PctBar value={Number(r[c])} /> : show(c, r[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A single-series bar (one brand hue) with the exact value as text beside it. */
function PctBar({ value }: { value: number }) {
  const width = Math.max(0, Math.min(100, value));
  return (
    <span className="flex items-center gap-2">
      <span aria-hidden className="h-2 w-20 shrink-0 rounded-full bg-brand-soft">
        <span className="block h-2 rounded-full bg-brand" style={{ width: `${width}%` }} />
      </span>
      <span className="tabular-nums text-ink">{value}%</span>
    </span>
  );
}
