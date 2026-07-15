// Typed API client for the inventory Stock surface (Sprint 6 FE).
//
// Thin wrappers over the REAL backend routes (verified against
// app/modules/inventory/router.py, all under the /api/v1 prefix):
//   GET  /inventory/items                              — list with computed on_hand + status
//   POST /inventory/items/{item_id}/opening-balance    — set starting stock
//
// The opening balance is idempotent SERVER-SIDE: the movement's idempotency key is
// derived as `opening_balance:{inventory_item_id}`, so a repeat POST returns the
// original movement and never double-adds stock. No Idempotency-Key header needed.
//
// TYPE-PIN: response types are hand-mirrored from the router's JSONResponse payloads
// (same caveat as recipes.ts — the server's own validation is the enforcement backstop).

import { API_BASE } from '../auth/config';
import { tenantHeader } from './activeTenant';
import { tryRefresh } from '../auth/session';

export class ItemsApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ItemsApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function req<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  const doFetch = (auth: string) =>
    fetch(`${API_BASE}/api/v1${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${auth}`,
        ...tenantHeader(), // X-Tenant-Id — required by resolve_principal (400 without)
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...(init?.headers ?? {}),
      },
    });
  let res = await doFetch(token);
  if (res.status === 401) {
    const fresh = await tryRefresh();
    if (fresh) res = await doFetch(fresh);
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new ItemsApiError(res.status, body.detail ?? `Request failed (${res.status})`);
  }
  return (await res.json().catch(() => undefined)) as T;
}

// ── Types (mirror app/modules/inventory/router.py list_inventory_items) ─────────────────
export type StockStatus =
  | 'ok'
  | 'low_stock'
  | 'out_of_stock'
  | 'count_required'
  | 'count_stale'
  | 'count_expired';

export type StockItem = {
  id: string;
  name: string;
  inventory_mode: 'recipe_deducted' | 'count_anchored';
  on_hand: number | null; // null = Mode B item with no count anchor yet
  stock_status: StockStatus;
  count_required: boolean;
  count_state: 'fresh' | 'stale' | 'expired' | null;
  low_stock: boolean | null;
  out_of_stock: boolean | null;
  par_level: number | null;
  last_count_at: string | null;
};

export type StockList = { items: StockItem[]; total: number };

export type OpeningBalanceResult = {
  movement_id: string;
  inventory_item_id: string;
  quantity: string;
};

// ── Endpoints ────────────────────────────────────────────────────────────────────────────
export const getItems = (token: string) => req<StockList>(token, '/inventory/items');

export const postOpeningBalance = (token: string, itemId: string, quantity: number) =>
  req<OpeningBalanceResult>(token, `/inventory/items/${itemId}/opening-balance`, {
    method: 'POST',
    body: JSON.stringify({ quantity }),
  });
