/**
 * REST helpers for the cost-ledger endpoints introduced in Phase 6
 * commit 46. Mirrors
 * ``media_engine/api/cost_routes.py:CostSummaryResponse`` +
 * ``CostLogResponse``.
 */

import { api } from './client';

export type CostGroupBy = 'op' | 'backend' | 'namespace';

export const COST_GROUP_BY: readonly CostGroupBy[] = [
  'op',
  'backend',
  'namespace',
];

export type CostRollupRow = {
  key: string;
  count: number;
  total_cents: number;
  total_usd: number;
  tokens_in: number;
  tokens_out: number;
};

export type CostSummaryResponse = {
  rows: CostRollupRow[];
  total_cents: number;
  group_by: CostGroupBy;
  since: string | null;
  until: string | null;
};

export type CostLogItem = {
  id: string;
  ts: string;
  op_name: string;
  backend_name: string | null;
  namespace: string;
  estimated_cents: number;
  actual_cents: number;
  tokens_in: number;
  tokens_out: number;
  duration_seconds: number | null;
};

export type CostLogResponse = {
  items: CostLogItem[];
  next_offset: number | null;
  limit: number;
  offset: number;
};

export type SummaryQuery = {
  group_by?: CostGroupBy;
  since?: string;
  until?: string;
};

export type LogQuery = {
  since?: string;
  until?: string;
  op?: string;
  limit?: number;
  offset?: number;
};

function buildQs(params: Record<string, string | number | undefined>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    qs.set(k, String(v));
  }
  const tail = qs.toString();
  return tail ? `?${tail}` : '';
}

export function fetchCostSummary(
  q: SummaryQuery = {},
): Promise<CostSummaryResponse> {
  return api.get<CostSummaryResponse>(`/cost/summary${buildQs(q)}`);
}

export function fetchCostLog(q: LogQuery = {}): Promise<CostLogResponse> {
  return api.get<CostLogResponse>(`/cost/log${buildQs(q)}`);
}

/**
 * Format a UTC ISO timestamp as a `YYYY-MM-DDTHH:mm` value suitable
 * for `<input type="datetime-local">`. The input element rejects the
 * `Z` suffix + sub-second precision so we strip both. The user reads
 * the value as *local* wall-clock time per the datetime-local spec,
 * so we render via `Date.prototype.get*` (local) rather than `getUTC*`.
 */
export function isoToLocalInputValue(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/**
 * Inverse of {@link isoToLocalInputValue}: parse a datetime-local
 * value (treated as the user's local time per the HTML spec) and
 * return the equivalent UTC ISO string. Empty string in → empty out.
 */
export function localInputValueToIso(local: string): string {
  if (!local) return '';
  // `new Date('YYYY-MM-DDTHH:mm')` parses as local time in every
  // browser — the same semantics as the datetime-local input itself.
  const d = new Date(local);
  if (Number.isNaN(d.getTime())) return '';
  return d.toISOString();
}

/**
 * Linear monthly extrapolation: total_cents over the window scaled to
 * a 30-day month. Returns the projected USD spend. Zero spend returns
 * a literal $0 (a useful answer); sub-minute or unparseable windows
 * return null so the caller can show "—" instead of a misleading huge
 * projection.
 */
export function monthlyBurnProjection(
  total_cents: number,
  windowStartIso: string,
  windowEndIso: string,
): number | null {
  if (total_cents <= 0) return 0;
  const start = new Date(windowStartIso).getTime();
  const end = new Date(windowEndIso).getTime();
  const windowMs = end - start;
  if (!Number.isFinite(windowMs) || windowMs < 60_000) return null;
  const MONTH_MS = 30 * 24 * 60 * 60 * 1000;
  return (total_cents / 100) * (MONTH_MS / windowMs);
}
