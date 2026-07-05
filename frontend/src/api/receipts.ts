// Typed API client for the Sprint 6 receipts surface (spec §7 FE Phase A).
//
// Thin wrappers over the REAL backend routes (verified against
// app/modules/receipts/router.py, all under /api/v1). Mobile upload is
// API-mediated (D-606-14): `uploadReceiptPhoto` POSTs the photo bytes as
// multipart to the server, which validates magic bytes + strips EXIF before its
// single Spaces PUT — there is NO presigned-PUT helper by design.
//
// Channel/intake-config endpoints (inbound-address, Gmail, invoice-senders) are
// deliberately ABSENT: those backend routes don't exist yet (Postmark/Gmail are
// later PRs), and the client↔server contract test fails any path the server
// doesn't expose.
//
// TYPE-PIN: types are hand-mirrored from app/modules/receipts/schemas.py (same
// caveat as recipes.ts — the server's own validation is the enforcement backstop).

import { API_BASE } from '../auth/config';

// ── Error type: stable codes let the UI branch (RECEIPT_REVIEW_REQUIRED, ...) ──
export class ReceiptApiError extends Error {
  status: number;
  code: string | null;
  detail: string;
  constructor(status: number, code: string | null, detail: string) {
    super(detail);
    this.name = 'ReceiptApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function req<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api/v1${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      // JSON content-type ONLY for string bodies — a FormData body must keep the
      // fetch-generated multipart boundary (uploadReceiptPhoto).
      ...(typeof init?.body === 'string' ? { 'Content-Type': 'application/json' } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as {
      detail?: string | { code?: string; message?: string };
    };
    const d = body.detail;
    const code = typeof d === 'object' && d?.code ? d.code : null;
    const message =
      typeof d === 'string' ? d : (d?.message ?? `Request failed (${res.status})`);
    throw new ReceiptApiError(res.status, code, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json().catch(() => undefined)) as T;
}

// ── Types (mirror app/modules/receipts/schemas.py) ───────────────────────────
export type CommitState = 'draft' | 'pending_review' | 'committed' | 'dismissed' | 'cancelled';
export type ExtractionStatus =
  | 'none'
  | 'pending'
  | 'processing'
  | 'complete'
  | 'failed'
  | 'manual_required'
  | 'superseded';
export type MatchStatus = 'unmatched' | 'matched' | 'created' | 'skipped';
export type ReceiptSource = 'mobile_photo' | 'gmail' | 'email' | 'webhook' | 'manual' | 'pos';

export type ItemSuggestion = { id: string; name: string };

export type ReceiptLine = {
  id: string;
  extracted_name: string | null;
  inventory_item_id: string | null;
  received_quantity: number | null;
  extracted_unit: string | null;
  unit_cost_cents: number | null;
  confidence: number | null;
  manually_corrected: boolean;
  match_status: MatchStatus;
  line_ordinal: number | null;
  suggestions: ItemSuggestion[];
};

export type ReceiptListItem = {
  id: string;
  source: ReceiptSource;
  commit_state: CommitState;
  extraction_status: ExtractionStatus;
  supplier_name: string | null;
  total_cents: number | null;
  manual_entry_required: boolean;
  quota_blocked: boolean;
  created_at: string;
};

export type ReceiptNote = {
  id: string;
  user_id: string;
  text: string;
  created_at: string;
};

export type ReceiptDetail = ReceiptListItem & {
  photo_object_key: string | null;
  photo_url: string | null;
  mime_type: string | null;
  invoice_number: string | null;
  invoice_date: string | null;
  subtotal_cents: number | null;
  tax_cents: number | null;
  extraction_confidence: number | null;
  review_visibility_status: string;
  sender_email: string | null;
  filter_flags: string[];
  reviewed_affirmation: boolean;
  review_started_at: string | null;
  notes_log: ReceiptNote[];
  lines: ReceiptLine[];
};

export type UploadResponse = {
  receipt_id: string;
  photo_object_key: string;
  mime_type: string;
  extraction_status: string;
};

/** PATCH-style line edit (PUT). Field ABSENT = unchanged; inventory_item_id
 * explicit null = clear (→ unmatched); `skipped` must travel alone. */
export type LineUpdatePayload = {
  inventory_item_id?: string | null;
  new_item_name?: string;
  new_item_unit?: string;
  received_quantity?: number;
  extracted_unit?: string;
  unit_cost_cents?: number | null;
  extracted_name?: string;
  skipped?: boolean;
};

export type LineCreatePayload = {
  extracted_name: string;
  received_quantity: number;
  extracted_unit: string;
  unit_cost_cents?: number | null;
  inventory_item_id?: string | null;
};

// ── Receipts ──────────────────────────────────────────────────────────────────
export const listReceipts = (
  token: string,
  filters?: { commit_state?: CommitState; extraction_status?: ExtractionStatus },
) => {
  const q = new URLSearchParams();
  if (filters?.commit_state) q.set('commit_state', filters.commit_state);
  if (filters?.extraction_status) q.set('extraction_status', filters.extraction_status);
  const qs = q.toString();
  const path = qs ? `/receipts?${qs}` : '/receipts';
  return req<ReceiptListItem[]>(token, path);
};

export const getReceipt = (token: string, receiptId: string) =>
  req<ReceiptDetail>(token, `/receipts/${receiptId}`);

/** API-mediated photo upload (D-606-14): bytes go THROUGH the server. */
export const uploadReceiptPhoto = (
  token: string,
  photo: { uri: string; fileName?: string | null; mimeType?: string | null },
) => {
  const form = new FormData();
  // RN FormData file part: {uri, name, type}.
  form.append('file', {
    uri: photo.uri,
    name: photo.fileName ?? 'receipt.jpg',
    type: photo.mimeType ?? 'image/jpeg',
  } as unknown as Blob);
  return req<UploadResponse>(token, `/receipts/uploads`, { method: 'POST', body: form });
};

export const createManualReceipt = (
  token: string,
  body: { supplier_name?: string | null; invoice_date?: string | null },
) => req<UploadResponse>(token, `/receipts`, { method: 'POST', body: JSON.stringify(body) });

/** 409 RECEIPT_REVIEW_IN_PROGRESS once review started — use resetExtraction. */
export const extractReceipt = (token: string, receiptId: string) =>
  req<{ job_id: string; status: string }>(token, `/receipts/${receiptId}/extract`, {
    method: 'POST',
  });

/** Destructive start-over; requires discardEdits=true or the server 409s. */
export const resetExtraction = (token: string, receiptId: string, discardEdits: boolean) =>
  req<{ job_id: string; status: string }>(token, `/receipts/${receiptId}/reset-extraction`, {
    method: 'POST',
    body: JSON.stringify({ discard_edits: discardEdits }),
  });

export const updateLine = (
  token: string,
  receiptId: string,
  lineId: string,
  patch: LineUpdatePayload,
) =>
  req<ReceiptLine>(token, `/receipts/${receiptId}/lines/${lineId}`, {
    method: 'PUT',
    body: JSON.stringify(patch),
  });

export const addLine = (token: string, receiptId: string, body: LineCreatePayload) =>
  req<ReceiptLine>(token, `/receipts/${receiptId}/lines`, {
    method: 'POST',
    body: JSON.stringify(body),
  });

export const addNote = (token: string, receiptId: string, text: string) =>
  req<ReceiptNote>(token, `/receipts/${receiptId}/notes`, {
    method: 'POST',
    body: JSON.stringify({ text }),
  });

/** Manager+. Throws ReceiptApiError with code RECEIPT_REVIEW_REQUIRED /
 * RECEIPT_NOTHING_TO_COMMIT / RECEIPT_UNIT_CONVERSION (422), or 403 for staff. */
export const commitReceipt = (token: string, receiptId: string, reviewedAffirmation: boolean) =>
  req<{ receipt_id: string; status: string; movement_ids: string[]; confirmed: boolean }>(
    token,
    `/receipts/${receiptId}/commit`,
    {
      method: 'POST',
      body: JSON.stringify({ confirm: true, reviewed_affirmation: reviewedAffirmation }),
    },
  );

export const cancelReceipt = (token: string, receiptId: string) =>
  req<{ status: string }>(token, `/receipts/${receiptId}/cancel`, { method: 'POST' });

export const dismissReceipt = (token: string, receiptId: string, reason: string) =>
  req<{ status: string }>(token, `/receipts/${receiptId}/dismiss`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  });
