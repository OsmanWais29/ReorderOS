"""Sprint 6 Phase 0 (part 1 of 2) — receipts/extraction schema, ADDITIVE only.

Revision ID: 0023_sprint6_receipts_schema
Revises: 0022_sprint5_force_rls
Create Date: 2026-06-28 00:00:00.000000

This is the **metadata, batchable** half of Sprint 6 Phase 0 (Migration Risk
Standard §2.1): all-new tables + additive columns on `receipts`/`receipt_lines`
that are either nullable or carry a DEFAULT, plus RLS/grants. It validates NO
existing data and creates NO trigger.

The **data-validating** half — backfilling `source`/`commit_state`/`match_status`
from real legacy state, making `receipt_lines.inventory_item_id` nullable, widening
the `commit_state` CHECK, `SET NOT NULL` on `source`, and creating
`trg_receipt_commit_integrity` LAST — lives in the isolated, preflight-carrying
`0024_sprint6_commit_integrity` (matching how Sprint 5 split 0014 from 0016). Keep
them as two migrations: never create the integrity trigger in the same migration
that backfills, or it fires on its own backfill.

Schema reconciliation vs the EXISTING 0003 tables (the spec calls these "extend
columns" — the real deltas):
  - `receipts.notes` is already TEXT (0003). The spec's append-only JSONB log is
    added as a NEW column `notes_log JSONB` (founder decision 2026-06-28); the
    TEXT `notes` is left untouched. No in-place type change, no data migration.
  - `receipts.commit_state` / `committed_at` already exist (0003). commit_state's
    CHECK is widened in 0024 (adds 'pending_review','dismissed'); committed_at is
    reused as-is and is NOT re-added here.
  - `source` is added here as NULLABLE with its CHECK (NULL passes a CHECK IN);
    0024 backfills it from legacy signals and SET NOT NULLs it.

RLS posture (Sprint 6 §3.4 + the shipped 0022/0003 convention): every new tenant
table is ENABLE **+ FORCE** (so a future table that forgets FORCE fails the
test_every_tenant_table_is_force_rls guard). Worker-claim tables
(receipt_extraction_jobs, inbound_email_inbox, inbound_email_attachments,
tenant_extraction_rate_limits, and the token/connection tables the webhook/worker
resolve cross-tenant) carry a `service_worker USING(true)` policy ALONGSIDE the
app_user _T1 policy — FORCE only binds the table OWNER, so the non-owner
service_worker role is governed by its USING(true) policy and can still claim
across tenants. This keeps the guard allowlist at exactly the two pre-existing
inbox tables (pos_event_inbox, tenant_pos_connections) — no new exemptions.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:             Low — new tables + additive cols (nullable/DEFAULT);
                                     zero existing rows validated.
  - Availability impact:       Low — CREATE TABLE/INDEX + ADD COLUMN (with DEFAULT,
                                     PG ≥11 fast-path); brief metadata locks only.
  - Application compatibility: Low — additive; no code reads these until Phase 2+.
  - Data propagation risk:     Low — no replication-affecting change.
  - Reversibility:             Low — full DROP / DROP COLUMN in downgrade().
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_sprint6_receipts_schema"
down_revision: str | None = "0022_sprint5_force_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tenant-isolation predicate — project-wide convention (0011/0013/0014).
_T1 = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _enable_force(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def _app_policy(table: str, name: str) -> None:
    """Tenant-scoped FOR ALL policy for the request-path role (capability gated by GRANT)."""
    op.execute(
        f"CREATE POLICY {name} ON {table} FOR ALL TO app_user "
        f"USING ({_T1}) WITH CHECK ({_T1})"
    )


def _sw_policy(table: str, name: str) -> None:
    """Cross-tenant USING(true) policy for the webhook/worker role (mirrors pos_event_inbox)."""
    op.execute(f"CREATE POLICY {name} ON {table} FOR ALL TO service_worker USING (true)")


def upgrade() -> None:  # noqa: PLR0915
    # ═════════════════════════════════════════════════════════════════════════
    # INBOUND_EMAIL_INBOX — durable Postmark/Gmail intake inbox (mirrors
    # pos_event_inbox). tenant_id NULLABLE: unknown-token Postmark rows are stored
    # for ops/alerting before any tenant is resolved (Sprint 6 §3.3). Four-state
    # processing_status; suppression lives on receipts, not here (v6.8).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE inbound_email_inbox (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid REFERENCES tenants(id),
            postmark_message_id text,
            gmail_message_id    text,
            source              text NOT NULL CHECK (source IN ('postmark', 'gmail')),
            mailbox_hash        text,
            from_email          text,
            subject             text,
            received_at         timestamptz,
            attachment_count    integer NOT NULL DEFAULT 0,
            has_html_body       boolean NOT NULL DEFAULT false,
            html_receipt_id     uuid UNIQUE,
            locked_at           timestamptz,
            lease_token         uuid,
            processing_status   text NOT NULL DEFAULT 'receiving'
                                CHECK (processing_status IN (
                                    'receiving', 'pending', 'processing', 'complete',
                                    'no_attachment', 'failed', 'failed_terminal', 'filtered_out')),
            suppression_stage   text CHECK (suppression_stage IN ('pre_draft')),
            skip_reason         text,
            filter_flags        jsonb NOT NULL DEFAULT '[]'::jsonb,
            last_error          text,
            attempts            integer NOT NULL DEFAULT 0,
            created_at          timestamptz NOT NULL DEFAULT now(),
            -- v6.10 #1: pre-draft suppression only ever pairs with filtered_out.
            -- NULL-safe (IS NOT DISTINCT FROM): a CHECK only REJECTS on explicit
            -- FALSE, so a plain `= 'pre_draft'` would let `filtered_out` + NULL
            -- stage through as UNKNOWN. (The spec's verbatim CHECK has this hole.)
            CONSTRAINT inbox_suppression_stage_check CHECK (
                (processing_status = 'filtered_out'
                    AND suppression_stage IS NOT DISTINCT FROM 'pre_draft')
                OR (processing_status <> 'filtered_out' AND suppression_stage IS NULL)
            )
        )
    """)
    # Tenant-scoped dedup (#11). Two partial uniques: known-tenant rows dedup on
    # (tenant_id, message_id); unknown-token Postmark rows (tenant_id NULL) dedup
    # on message_id alone so a misconfigured retried forward makes ONE alert row.
    op.execute("""
        CREATE UNIQUE INDEX uq_inbox_postmark_msg
            ON inbound_email_inbox (tenant_id, postmark_message_id)
            WHERE tenant_id IS NOT NULL AND postmark_message_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_inbox_postmark_msg_unknown
            ON inbound_email_inbox (postmark_message_id)
            WHERE tenant_id IS NULL AND postmark_message_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_inbox_gmail_msg
            ON inbound_email_inbox (tenant_id, gmail_message_id)
            WHERE gmail_message_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX idx_inbox_claim
            ON inbound_email_inbox (processing_status, locked_at)
    """)
    _enable_force("inbound_email_inbox")
    _app_policy("inbound_email_inbox", "inbox_email_tenant_isolation")
    _sw_policy("inbound_email_inbox", "inbox_email_service_access")
    op.execute("GRANT SELECT ON inbound_email_inbox TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE ON inbound_email_inbox TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # RECEIPT_EXTRACTION_JOBS — extraction worker queue (mirrors pos_event_inbox).
    # Claimed cross-tenant by the extraction worker; enqueued by the /extract
    # endpoint (app_user) and the intake workers (service_worker). Lease + fence
    # columns (locked_at/lease_token) per §3.3.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE receipt_extraction_jobs (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id),
            receipt_id          uuid NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
            job_attempt         integer NOT NULL DEFAULT 1,
            status              text NOT NULL DEFAULT 'pending'
                                CHECK (status IN (
                                    'pending', 'processing', 'complete', 'failed',
                                    'failed_terminal', 'quota_blocked', 'skipped', 'superseded')),
            locked_at           timestamptz,
            lease_token         uuid,
            quota_blocked_until timestamptz,
            attempts            integer NOT NULL DEFAULT 0,
            last_error          text,
            raw_extraction      jsonb,
            started_at          timestamptz,
            completed_at        timestamptz,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX idx_extraction_jobs_tenant_status
            ON receipt_extraction_jobs (tenant_id, status)
    """)
    op.execute("""
        CREATE INDEX idx_extraction_jobs_claim
            ON receipt_extraction_jobs (status, locked_at)
    """)
    _enable_force("receipt_extraction_jobs")
    _app_policy("receipt_extraction_jobs", "extraction_jobs_tenant_isolation")
    _sw_policy("receipt_extraction_jobs", "extraction_jobs_service_access")
    # app_user enqueues (POST /extract) and polls status; never claims/updates.
    op.execute("GRANT SELECT, INSERT ON receipt_extraction_jobs TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE ON receipt_extraction_jobs TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # INBOUND_EMAIL_ATTACHMENTS — one row per QUALIFIED attachment (D-606-01 /
    # #12). receipt_id set once the draft is fanned out — the idempotency guard:
    # a crash mid-fanout retries only rows still NULL. UNIQUE(inbound_email_id,
    # attachment_index) is the second half of that guard.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE inbound_email_attachments (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            inbound_email_id  uuid NOT NULL REFERENCES inbound_email_inbox(id) ON DELETE CASCADE,
            tenant_id         uuid NOT NULL REFERENCES tenants(id),
            attachment_index  integer NOT NULL,
            original_filename text,
            mime_type         text,
            object_key        text,
            receipt_id        uuid REFERENCES receipts(id) ON DELETE SET NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_inbound_attachment UNIQUE (inbound_email_id, attachment_index)
        )
    """)
    _enable_force("inbound_email_attachments")
    _app_policy("inbound_email_attachments", "inbound_attach_tenant_isolation")
    _sw_policy("inbound_email_attachments", "inbound_attach_service_access")
    op.execute("GRANT SELECT ON inbound_email_attachments TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE ON inbound_email_attachments TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # TENANT_EXTRACTION_RATE_LIMITS — per-tenant daily cap (D-606-23). Charged by
    # the extraction worker cross-tenant via an atomic UTC upsert.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE tenant_extraction_rate_limits (
            tenant_id         uuid PRIMARY KEY REFERENCES tenants(id),
            daily_cap         integer NOT NULL DEFAULT 50,
            jobs_today        integer NOT NULL DEFAULT 0,
            window_started_at date,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    _enable_force("tenant_extraction_rate_limits")
    _app_policy("tenant_extraction_rate_limits", "extraction_rl_tenant_isolation")
    _sw_policy("tenant_extraction_rate_limits", "extraction_rl_service_access")
    op.execute("GRANT SELECT ON tenant_extraction_rate_limits TO app_user")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON tenant_extraction_rate_limits TO service_worker"
    )

    # ═════════════════════════════════════════════════════════════════════════
    # TENANT_INBOUND_EMAIL_TOKENS — Postmark MailboxHash routing. Resolved
    # cross-tenant by the webhook (service_worker SELECT); managed by app_user.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE tenant_inbound_email_tokens (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  uuid NOT NULL REFERENCES tenants(id),
            token      text NOT NULL UNIQUE,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_inbound_email_tokens_tenant ON tenant_inbound_email_tokens (tenant_id)")
    _enable_force("tenant_inbound_email_tokens")
    _app_policy("tenant_inbound_email_tokens", "inbound_email_tokens_tenant_isolation")
    _sw_policy("tenant_inbound_email_tokens", "inbound_email_tokens_service_access")
    op.execute("GRANT SELECT, INSERT, UPDATE ON tenant_inbound_email_tokens TO app_user")
    op.execute("GRANT SELECT ON tenant_inbound_email_tokens TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # TENANT_INBOUND_WEBHOOK_TOKENS — generic webhook Bearer (hashed, constant-time
    # compared). Validated cross-tenant by the webhook (service_worker SELECT).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE tenant_inbound_webhook_tokens (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  uuid NOT NULL REFERENCES tenants(id),
            token_hash text NOT NULL,
            revoked_at timestamptz,
            created_by uuid REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_inbound_webhook_tokens_tenant ON tenant_inbound_webhook_tokens (tenant_id)")
    _enable_force("tenant_inbound_webhook_tokens")
    _app_policy("tenant_inbound_webhook_tokens", "inbound_webhook_tokens_tenant_isolation")
    _sw_policy("tenant_inbound_webhook_tokens", "inbound_webhook_tokens_service_access")
    op.execute("GRANT SELECT, INSERT, UPDATE ON tenant_inbound_webhook_tokens TO app_user")
    op.execute("GRANT SELECT ON tenant_inbound_webhook_tokens TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # TENANT_GMAIL_CONNECTIONS — WorkOS Pipes link. Polled cross-tenant by the
    # gmail sync worker (service_worker SELECT/UPDATE for last_sync/status);
    # connect/disconnect by app_user. No history cursor (D-606-19 non-incremental).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE tenant_gmail_connections (
            id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenants(id),
            connected_by_user_id    uuid REFERENCES users(id),
            workos_user_id          text,
            status                  text NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active', 'needs_reauthorization', 'disconnected')),
            gmail_address           text,
            last_sync_at            timestamptz,
            first_sync_completed_at timestamptz,
            sync_enabled            boolean NOT NULL DEFAULT true,
            setup_completed_at      timestamptz,
            sync_mode               text NOT NULL DEFAULT 'label_only'
                                    CHECK (sync_mode IN ('label_only')),
            created_at              timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_gmail_connections_tenant ON tenant_gmail_connections (tenant_id)")
    _enable_force("tenant_gmail_connections")
    _app_policy("tenant_gmail_connections", "gmail_connections_tenant_isolation")
    _sw_policy("tenant_gmail_connections", "gmail_connections_service_access")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_gmail_connections TO app_user")
    op.execute("GRANT SELECT, UPDATE ON tenant_gmail_connections TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # TENANT_INVOICE_SENDERS — sender allowlist (Layer 2). app_user CRUD; worker
    # reads for query-building / Postmark allowlist annotation.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE tenant_invoice_senders (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     uuid NOT NULL REFERENCES tenants(id),
            match_type    text NOT NULL CHECK (match_type IN ('email', 'domain')),
            match_value   text NOT NULL,
            supplier_name text,
            created_by    uuid REFERENCES users(id),
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX idx_invoice_senders_tenant ON tenant_invoice_senders (tenant_id, match_type, match_value)
    """)
    _enable_force("tenant_invoice_senders")
    _app_policy("tenant_invoice_senders", "invoice_senders_tenant_isolation")
    _sw_policy("tenant_invoice_senders", "invoice_senders_service_access")
    op.execute("GRANT SELECT, INSERT, DELETE ON tenant_invoice_senders TO app_user")
    op.execute("GRANT SELECT ON tenant_invoice_senders TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # TENANT_ACTIVE_EMAIL_CHANNEL — single active email channel per tenant
    # (D-606-18). UNIQUE(tenant_id) makes two active channels impossible. App-only.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE tenant_active_email_channel (
            tenant_id    uuid PRIMARY KEY REFERENCES tenants(id),
            channel_type text NOT NULL CHECK (channel_type IN ('gmail', 'postmark')),
            channel_ref  uuid NOT NULL,
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    _enable_force("tenant_active_email_channel")
    _app_policy("tenant_active_email_channel", "active_email_channel_tenant_isolation")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_active_email_channel TO app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # RECEIPT_ADJUSTMENTS — post-commit corrections (append-only, Phase 5).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE receipt_adjustments (
            id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               uuid NOT NULL REFERENCES tenants(id),
            receipt_id              uuid NOT NULL REFERENCES receipts(id),
            receipt_line_id         uuid REFERENCES receipt_lines(id),
            inventory_item_id       uuid NOT NULL REFERENCES inventory_items(id),
            adjustment_type         text NOT NULL
                                    CHECK (adjustment_type IN ('correction', 'return', 'damage', 'count_fix')),
            delta_quantity          numeric NOT NULL,
            delta_unit              text NOT NULL,
            reason                  text,
            delta_cost_cents        integer,
            compensating_movement_id uuid REFERENCES inventory_movements(id),
            created_by              uuid REFERENCES users(id),
            created_at              timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_receipt_adjustments_receipt ON receipt_adjustments (tenant_id, receipt_id)")
    _enable_force("receipt_adjustments")
    _app_policy("receipt_adjustments", "receipt_adjustments_tenant_isolation")
    # Append-only: SELECT + INSERT, no UPDATE/DELETE grant.
    op.execute("GRANT SELECT, INSERT ON receipt_adjustments TO app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # RECEIPTS — additive columns (Sprint 6 §3.1). All nullable or DEFAULT'd.
    # `source` added NULLABLE here (CHECK passes on NULL); 0024 backfills + NOT NULL.
    # `notes_log` is the NEW JSONB append-only column; the existing TEXT `notes`
    # and `committed_at` are left as-is.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        ALTER TABLE receipts
            ADD COLUMN photo_object_key         text,
            ADD COLUMN source                   text
                CHECK (source IN ('mobile_photo', 'gmail', 'email', 'webhook', 'manual', 'pos')),
            ADD COLUMN invoice_number           text,
            ADD COLUMN invoice_date             date,
            ADD COLUMN subtotal_cents           integer,
            ADD COLUMN tax_cents                integer,
            ADD COLUMN total_cents              integer,
            ADD COLUMN original_filename        text,
            ADD COLUMN mime_type                text,
            ADD COLUMN file_size_bytes          integer,
            ADD COLUMN notes_log                jsonb NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN dismissed_reason         text,
            ADD COLUMN extraction_status        text NOT NULL DEFAULT 'none'
                CHECK (extraction_status IN (
                    'none', 'pending', 'processing', 'complete', 'failed',
                    'manual_required', 'superseded')),
            ADD COLUMN extraction_confidence    numeric(4, 3),
            ADD COLUMN review_visibility_status text NOT NULL DEFAULT 'visible'
                CHECK (review_visibility_status IN ('visible', 'suppressed')),
            ADD COLUMN suppression_reason       text CHECK (suppression_reason IN ('not_invoice')),
            ADD COLUMN supplier_name            text,
            ADD COLUMN quota_blocked            boolean NOT NULL DEFAULT false,
            ADD COLUMN quota_blocked_until      timestamptz,
            ADD COLUMN confirmed_at             timestamptz,
            ADD COLUMN reviewed_affirmation     boolean NOT NULL DEFAULT false,
            ADD COLUMN reviewed_at              timestamptz,
            ADD COLUMN review_started_at        timestamptz,
            ADD COLUMN manual_entry_required    boolean NOT NULL DEFAULT false,
            ADD COLUMN inbound_email_id         uuid REFERENCES inbound_email_inbox(id),
            ADD COLUMN sender_email             text,
            ADD COLUMN filter_flags             jsonb NOT NULL DEFAULT '[]'::jsonb,
            -- NULL-safe: `suppression_reason = 'not_invoice'` yields UNKNOWN (not
            -- FALSE) when the reason is NULL, and a CHECK passes on UNKNOWN — so a
            -- plain `=` would let `suppressed` + NULL reason through. IS NOT
            -- DISTINCT FROM returns a real boolean. (Spec's verbatim CHECK has this hole.)
            ADD CONSTRAINT receipts_review_visibility_reason_check CHECK (
                (review_visibility_status = 'visible'    AND suppression_reason IS NULL)
                OR (review_visibility_status = 'suppressed'
                    AND suppression_reason IS NOT DISTINCT FROM 'not_invoice')
            )
    """)
    op.execute("CREATE INDEX idx_receipts_source_state ON receipts (tenant_id, source, commit_state)")
    op.execute("CREATE INDEX idx_receipts_extraction_status ON receipts (tenant_id, extraction_status)")

    # ═════════════════════════════════════════════════════════════════════════
    # RECEIPT_LINES — additive columns (Sprint 6 §3.2). inventory_item_id stays
    # NOT NULL here; 0024 makes it nullable AFTER the committed-line preflight.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        ALTER TABLE receipt_lines
            ADD COLUMN extracted_name     text,
            ADD COLUMN confidence         numeric(4, 3),
            ADD COLUMN manually_corrected boolean NOT NULL DEFAULT false,
            ADD COLUMN match_status       text NOT NULL DEFAULT 'unmatched'
                CHECK (match_status IN ('unmatched', 'matched', 'created', 'skipped')),
            ADD COLUMN extraction_job_id  uuid REFERENCES receipt_extraction_jobs(id),
            ADD COLUMN job_attempt        integer,
            ADD COLUMN line_ordinal       integer
    """)
    # Job-keyed idempotency (#5/#13). NULL extraction_job_id (operator-added lines)
    # are treated as distinct by the unique index, so they never collide.
    op.execute("""
        CREATE UNIQUE INDEX uq_receipt_lines_job_ordinal
            ON receipt_lines (receipt_id, extraction_job_id, line_ordinal)
    """)


def downgrade() -> None:
    # receipt_lines / receipts additive columns first (drop FK-bearing cols before
    # the tables they reference).
    op.execute("DROP INDEX IF EXISTS uq_receipt_lines_job_ordinal")
    op.execute("""
        ALTER TABLE receipt_lines
            DROP COLUMN IF EXISTS extracted_name,
            DROP COLUMN IF EXISTS confidence,
            DROP COLUMN IF EXISTS manually_corrected,
            DROP COLUMN IF EXISTS match_status,
            DROP COLUMN IF EXISTS extraction_job_id,
            DROP COLUMN IF EXISTS job_attempt,
            DROP COLUMN IF EXISTS line_ordinal
    """)
    op.execute("DROP INDEX IF EXISTS idx_receipts_extraction_status")
    op.execute("DROP INDEX IF EXISTS idx_receipts_source_state")
    op.execute("ALTER TABLE receipts DROP CONSTRAINT IF EXISTS receipts_review_visibility_reason_check")
    op.execute("""
        ALTER TABLE receipts
            DROP COLUMN IF EXISTS photo_object_key,
            DROP COLUMN IF EXISTS source,
            DROP COLUMN IF EXISTS invoice_number,
            DROP COLUMN IF EXISTS invoice_date,
            DROP COLUMN IF EXISTS subtotal_cents,
            DROP COLUMN IF EXISTS tax_cents,
            DROP COLUMN IF EXISTS total_cents,
            DROP COLUMN IF EXISTS original_filename,
            DROP COLUMN IF EXISTS mime_type,
            DROP COLUMN IF EXISTS file_size_bytes,
            DROP COLUMN IF EXISTS notes_log,
            DROP COLUMN IF EXISTS dismissed_reason,
            DROP COLUMN IF EXISTS extraction_status,
            DROP COLUMN IF EXISTS extraction_confidence,
            DROP COLUMN IF EXISTS review_visibility_status,
            DROP COLUMN IF EXISTS suppression_reason,
            DROP COLUMN IF EXISTS supplier_name,
            DROP COLUMN IF EXISTS quota_blocked,
            DROP COLUMN IF EXISTS quota_blocked_until,
            DROP COLUMN IF EXISTS confirmed_at,
            DROP COLUMN IF EXISTS reviewed_affirmation,
            DROP COLUMN IF EXISTS reviewed_at,
            DROP COLUMN IF EXISTS review_started_at,
            DROP COLUMN IF EXISTS manual_entry_required,
            DROP COLUMN IF EXISTS inbound_email_id,
            DROP COLUMN IF EXISTS sender_email,
            DROP COLUMN IF EXISTS filter_flags
    """)
    # New tables — reverse dependency order.
    op.execute("DROP TABLE IF EXISTS receipt_adjustments")
    op.execute("DROP TABLE IF EXISTS tenant_active_email_channel")
    op.execute("DROP TABLE IF EXISTS tenant_invoice_senders")
    op.execute("DROP TABLE IF EXISTS tenant_gmail_connections")
    op.execute("DROP TABLE IF EXISTS tenant_inbound_webhook_tokens")
    op.execute("DROP TABLE IF EXISTS tenant_inbound_email_tokens")
    op.execute("DROP TABLE IF EXISTS tenant_extraction_rate_limits")
    op.execute("DROP TABLE IF EXISTS inbound_email_attachments")
    op.execute("DROP TABLE IF EXISTS receipt_extraction_jobs")
    op.execute("DROP TABLE IF EXISTS inbound_email_inbox")
