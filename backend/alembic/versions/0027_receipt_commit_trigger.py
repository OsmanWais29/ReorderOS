"""Sprint 6 S4 — trg_receipt_commit_integrity (D-606-05), deferred from Phase 0.

Revision ID: 0027_receipt_commit_trigger
Revises: 0026_receipt_line_unit
Create Date: 2026-06-29 00:00:00.000000

The DB backstop for the commit gate. Created HERE (not Phase 0) because it ships
atomically with the commit_receipt upgrade — the upgraded function is what stamps
confirmed_at and enforces the affirmation, so the trigger and the code are two
halves of one gate (creating it earlier would break the un-upgraded manual commit).

Fires BEFORE UPDATE on receipts only on the draft→committed transition
(OLD.commit_state IS DISTINCT FROM 'committed' AND NEW='committed'; NULL-safe).

Branches on source, FAIL-CLOSED (D-606-05):
  - integrity floor (ALL sources, incl. pos): >=1 child line with
    match_status<>'skipped' AND inventory_item_id IS NOT NULL (D-606-06);
  - intake sources (mobile_photo/gmail/email/webhook/manual): confirmed_at NOT NULL
    AND (>=1 manually_corrected line OR reviewed_affirmation) (D-606-04/D-606-22);
  - source='pos': human-review guards BYPASSED (deterministic Clover walk — note: no
    code path creates pos-origin receipts in v1, so this branch is defensive only);
  - any other / NULL source: RAISE — an unrecognized source can never commit (the
    guard fails closed rather than silently skipping).

The function is SECURITY INVOKER (default): it runs in the committing request's RLS
context (app.tenant_id set), so its receipt_lines count sees the tenant's lines.

Risk: Low — trigger + function only; no data touched. Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_receipt_commit_trigger"
down_revision: str | None = "0026_receipt_line_unit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_receipt_commit_integrity_fn()
        RETURNS trigger AS $$
        DECLARE
            n_inventory_lines bigint;
            n_corrected_lines bigint;
        BEGIN
            -- Integrity floor (all sources): the receipt must move >=1 inventory line.
            SELECT count(*) INTO n_inventory_lines
              FROM receipt_lines
             WHERE receipt_id = NEW.id
               AND match_status <> 'skipped'
               AND inventory_item_id IS NOT NULL;
            IF n_inventory_lines = 0 THEN
                RAISE EXCEPTION
                    'receipt % cannot commit: no inventory-moving line (need >=1 line with '
                    'match_status<>skipped and inventory_item_id set)', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;

            IF NEW.source IN ('mobile_photo','gmail','email','webhook','manual') THEN
                IF NEW.confirmed_at IS NULL THEN
                    RAISE EXCEPTION
                        'receipt % cannot commit: confirmed_at must be server-set for source %',
                        NEW.id, NEW.source
                        USING ERRCODE = 'check_violation';
                END IF;
                SELECT count(*) INTO n_corrected_lines
                  FROM receipt_lines
                 WHERE receipt_id = NEW.id AND manually_corrected = true;
                IF n_corrected_lines = 0 AND NEW.reviewed_affirmation IS NOT TRUE THEN
                    RAISE EXCEPTION
                        'receipt % cannot commit: requires >=1 manually-corrected line or a '
                        'review affirmation (D-606-22)', NEW.id
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.source = 'pos' THEN
                NULL;  -- deterministic Clover walk; human-review guards intentionally bypassed
            ELSE
                RAISE EXCEPTION
                    'receipt % cannot commit: unrecognized source % (fail-closed)',
                    NEW.id, NEW.source
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_receipt_commit_integrity
            BEFORE UPDATE ON receipts
            FOR EACH ROW
            WHEN (OLD.commit_state IS DISTINCT FROM 'committed'
                  AND NEW.commit_state = 'committed')
            EXECUTE FUNCTION trg_receipt_commit_integrity_fn();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_receipt_commit_integrity ON receipts")
    op.execute("DROP FUNCTION IF EXISTS trg_receipt_commit_integrity_fn()")
