// Typed API client for the inbound-email observability surface (Sprint 6 3b).
//
// Thin wrappers over the REAL backend routes (verified against
// app/modules/receipts/inbound_admin.py, all under the /api/v1 prefix):
//   GET /receipts/inbound-address              — tenant forwarding address
//   GET /receipts/inbound-emails               — recent inbound emails (safe metadata)
//   GET /receipts/inbound-emails/{id}          — one email + attachments
//
// Responses are metadata-only by backend contract (D-606-15): no bodies, no
// attachment bytes, no routing tokens (except the forwarding address itself,
// which IS the product feature).

import { API_BASE } from '../auth/config';
import { tenantHeader } from './activeTenant';
import { tryRefresh } from '../auth/session';

export class InboundEmailsApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'InboundEmailsApiError';
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
    const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
    const detail =
      typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? '');
    throw new InboundEmailsApiError(res.status, detail || `Request failed (${res.status})`);
  }
  return (await res.json().catch(() => undefined)) as T;
}

// ── Types (mirror inbound_admin._serialize) ───────────────────────────────────
export type InboundDisplayStatus =
  | 'received'
  | 'processing'
  | 'filtered'
  | 'error'
  | 'draft_created'
  | 'needs_review'
  | 'committed';

export type InboundLinkedReceipt = {
  receipt_id: string;
  commit_state: string;
  extraction_status: string;
  manual_entry_required: boolean;
  review_visibility_status: string;
};

export type InboundEmail = {
  id: string;
  received_at: string | null;
  created_at: string;
  from_email: string | null;
  subject: string | null;
  source: string;
  processing_status: string;
  display_status: InboundDisplayStatus;
  skip_reason: string | null;
  filter_flags: string[];
  attachment_count: number;
  qualified_attachment_count: number;
  has_html_body: boolean;
  error_class: string | null;
  receipts: InboundLinkedReceipt[];
};

export type InboundEmailDetail = InboundEmail & {
  attachments: {
    attachment_index: number;
    original_filename: string | null;
    mime_type: string | null;
    stored: boolean;
    receipt_id: string | null;
  }[];
};

export type InboundAddress = { configured: boolean; address: string | null };

// ── Endpoints ─────────────────────────────────────────────────────────────────
export const getInboundAddress = (token: string) => {
  const path = '/receipts/inbound-address';
  return req<InboundAddress>(token, path);
};

export const listInboundEmails = (token: string) => {
  const path = '/receipts/inbound-emails';
  return req<{ inbound_emails: InboundEmail[] }>(token, path);
};

export const getInboundEmail = (token: string, inboundEmailId: string) =>
  req<InboundEmailDetail>(token, `/receipts/inbound-emails/${inboundEmailId}`);
