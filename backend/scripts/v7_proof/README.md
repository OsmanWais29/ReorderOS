# V7 proof & Clover debug tools

One-off manual tools used to prove the Sprint 5 depletion engine end-to-end against
a **real Clover sandbox** (the V7 verification chain). They are **not** part of the
runtime, deploy, or CI path — nothing in `app/` or `tests/` imports them. They are
kept (not deleted) because the upcoming multi-line + modifier live-cert and combo
work needs the cash-tender / trace / dump tools again.

The original-location snapshot (before these moved out of `backend/scripts/`) is
preserved at git tag **`v7-proof-tools`**.

## ⚠️ Tools that WRITE to live Clover

These mutate real Clover sandbox state — run deliberately, not casually:

- **`clover_cash_pay.py`** — creates a real order and CASH-tenders it to LOCKED.
- **`clover_create_item.py`** — creates a real sellable catalog item (via the app's stored OAuth token).
- **`clover_create_item_incontainer.py`** — same, run from inside the DO staging container.

Run `clover_scope_check.py` first so you don't burn a write attempt without the scopes.

## Read-only / diagnostic

- `clover_scope_check.py` — verify the stored token has Orders:Write + Payments:Write (write probe, non-committing).
- `clover_token_inspect.py` — inspect all stored Clover connection rows for the tenant.
- `clover_sync_debug.py` — diagnose why a catalog sync isn't landing an item.
- `dump_payload.py` — print the raw `fetched_payload` JSON for one order (fixture capture).
- `depletion_summary.py` — cross-order no-double-count proof over `sale_depletion` movements.
- `trace_depletion.py` — trace + verify depletion for one Clover order (the V7 end-to-end proof).
- `precondition_check.py` — V7 precondition check; run before ringing a sandbox sale.

## Running

From the `backend/` directory:

```
python -m scripts.v7_proof.<name>          # e.g. scripts.v7_proof.trace_depletion
# fallback if the 'app' import fails:
PYTHONPATH=. python scripts/v7_proof/<name>.py
```

(The module path changed from `scripts.<name>` to `scripts.v7_proof.<name>` when these
moved here; older docstrings inside the files may still show the old `scripts.<name>` path.)
