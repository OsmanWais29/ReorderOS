"""Sprint 6 Phase 0 (part 2 of 2) — data-validating tightening on receipts/lines.

Revision ID: 0024_sprint6_receipts_tighten
Revises: 0023_sprint6_receipts_schema
Create Date: 2026-06-28 00:00:00.000000

The ISOLATED data-validating half of Phase 0 (Migration Risk Standard §2.2): it
backfills legacy rows from real state, widens a CHECK, and tightens nullability on
two PRE-EXISTING tables (receipts, receipt_lines). Per §3 it opens with a
count-and-stop preflight. Split from the additive 0023 exactly as Sprint 5 split
0016 from 0014.

DELIBERATE COHESION FIX — the integrity trigger is NOT created here.
--------------------------------------------------------------------
The Sprint 6 spec puts `trg_receipt_commit_integrity` (D-606-05) in Phase 0. But
the trigger requires intake-source commits to carry `confirmed_at` + the D-606-22
affirmation, and the EXISTING `commit_receipt` (inventory/services.py) — the
Sprint-3 manual path, source='manual' (an intake source) — sets NEITHER. Creating
the trigger before the Phase-4 commit upgrade would break every manual commit (and
its tests) in the S0→S4 window. The trigger and the commit upgrade are two halves
of ONE behavioral gate, so the trigger ships with the Phase-4 commit migration,
atomically with the upgraded `commit_receipt`. This migration only prepares the
schema the trigger will later read (match_status, source NOT NULL, nullable item).

Backfills are from REAL legacy state, never a blanket default (#2):
  - `source`: the ONLY code path that inserts `receipts` is the manual endpoint
    (`create_receipt`, inventory/services.py) — POS/depletion never create receipts.
    So every legacy receipt is manual-origin → `source='manual'`. (`create_receipt`
    is updated in the same change to set source explicitly, so SET NOT NULL is safe
    with no column DEFAULT — Sprint 5 no-default-on-cutover discipline.)
  - `match_status`: committed lines → 'matched' (they carry a non-null item under
    the old NOT NULL); draft lines keep the 0023 default 'unmatched'. Required so a
    committed legacy receipt satisfies the future trigger's existential check
    (`NULL <> 'skipped'` is `unknown` in SQL — an un-backfilled NULL would fail it).
  - `commit_state`: already holds real legacy values (draft/committed/cancelled,
    0003) — all valid under the WIDENED CHECK, so no value backfill, only widen.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:             Medium — tightens nullability + widens a CHECK on two
                                        existing tables; §3 preflight counts-and-stops.
  - Availability impact:       Low — brief metadata locks; no table scan beyond the
                                     SET NOT NULL validity check (empty/pilot-scale).
  - Application compatibility: Medium — `source` NOT NULL requires `create_receipt`
                                        to set it (done in this change); no other
                                        writer exists. Nullable item only LOOSENS.
  - Data propagation risk:     Low — no replication-affecting change.
  - Reversibility:             Medium — downgrade restores NOT NULL/old CHECK; safe
                                        only while no row uses the new states/NULL item
                                        (true pre-Phase-2). Documented below.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_sprint6_receipts_tighten"
down_revision: str | None = "0023_sprint6_receipts_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ═════════════════════════════════════════════════════════════════════════
    # PREFLIGHT (Migration Risk Standard §3) — count-and-stop BEFORE any change.
    # Accumulates every would-be violation and RAISEs once with the full breakdown.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        DO $$
        DECLARE
            v_msg text := '';
            n     bigint;
        BEGIN
            -- A committed line must carry an item (it raised inventory). Sanity
            -- before we DROP NOT NULL on inventory_item_id — a committed line with
            -- a NULL item would be irrecoverable legacy corruption.
            SELECT count(*) INTO n
              FROM receipt_lines l JOIN receipts r ON r.id = l.receipt_id
             WHERE r.commit_state = 'committed' AND l.inventory_item_id IS NULL;
            IF n > 0 THEN
                v_msg := v_msg || format('  committed receipt_lines with NULL inventory_item_id: %s%s', n, chr(10));
            END IF;

            -- A committed receipt with ZERO lines cannot satisfy the future
            -- integrity floor (>=1 inventory-moving line) — flag as legacy bad data.
            SELECT count(*) INTO n
              FROM receipts r
             WHERE r.commit_state = 'committed'
               AND NOT EXISTS (SELECT 1 FROM receipt_lines l WHERE l.receipt_id = r.id);
            IF n > 0 THEN
                v_msg := v_msg || format('  committed receipts with no lines: %s%s', n, chr(10));
            END IF;

            IF v_msg <> '' THEN
                RAISE EXCEPTION E'Migration 0024 preflight FAILED. Fix these rows before upgrading:\n%', v_msg;
            END IF;
        END $$;
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # BACKFILL from real legacy state (must precede the constraint tightening).
    # ═════════════════════════════════════════════════════════════════════════
    # Every legacy receipt is manual-origin (only create_receipt inserts receipts).
    op.execute("UPDATE receipts SET source = 'manual' WHERE source IS NULL")
    # Committed lines → 'matched' so the future trigger's existential check passes.
    op.execute("""
        UPDATE receipt_lines l
           SET match_status = 'matched'
          FROM receipts r
         WHERE l.receipt_id = r.id
           AND r.commit_state = 'committed'
           AND l.match_status = 'unmatched'
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # TIGHTEN — widen commit_state CHECK; source NOT NULL; loosen item nullability.
    # ═════════════════════════════════════════════════════════════════════════
    # Widen the 0003 CHECK ('draft','committed','cancelled') to the Sprint 6 set.
    op.execute("ALTER TABLE receipts DROP CONSTRAINT receipts_commit_state_check")
    op.execute("""
        ALTER TABLE receipts
            ADD CONSTRAINT receipts_commit_state_check
            CHECK (commit_state IN ('draft', 'pending_review', 'committed', 'dismissed', 'cancelled'))
    """)
    # source is now guaranteed non-NULL (backfill above + create_receipt sets it).
    op.execute("ALTER TABLE receipts ALTER COLUMN source SET NOT NULL")
    # Extraction creates lines before an item is matched → item must be nullable.
    op.execute("ALTER TABLE receipt_lines ALTER COLUMN inventory_item_id DROP NOT NULL")


def downgrade() -> None:
    # Restore NOT NULL on item (safe only while no extraction-draft NULL items
    # exist — true before Phase 2 ships).
    op.execute("ALTER TABLE receipt_lines ALTER COLUMN inventory_item_id SET NOT NULL")
    # source back to nullable.
    op.execute("ALTER TABLE receipts ALTER COLUMN source DROP NOT NULL")
    # Restore the original 0003 CHECK (safe only while no row uses the new states).
    op.execute("ALTER TABLE receipts DROP CONSTRAINT receipts_commit_state_check")
    op.execute("""
        ALTER TABLE receipts
            ADD CONSTRAINT receipts_commit_state_check
            CHECK (commit_state IN ('draft', 'committed', 'cancelled'))
    """)
    # Backfilled source='manual' / match_status='matched' values are valid 0023-state
    # values and are intentionally left in place (no lossy un-backfill).
