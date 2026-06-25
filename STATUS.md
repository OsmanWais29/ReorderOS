# ReorderOS — Status

Living snapshot of where the build actually is. Replaces the old point-in-time
`build-status-audit.md` and `clover-depletion-proof-status.md`. For the data-flow
map see [`ARCHITECTURE.md`](ARCHITECTURE.md); for failure modes / ops see
[`docs/sprint-failure-catalog.md`](docs/sprint-failure-catalog.md).

_Branch: `sprint-5-recipe-depletion`. Last updated: 2026-06-25._

## Headline

The backend value-loop (Clover sale → depletion → ledger) is **built and unit-/load-
proven**, but almost none of its *output* is visible in the app yet. **Depletion runs
correctly and invisibly.** The one remaining proof gap is founder-only: a real Clover
sandbox sale (everything below the engine line has only ever run against synthetic
payloads we authored).

## Proof status — graded against executed test runs

| Claim | Verdict |
|---|---|
| Depletion math correct | **PROVEN (engine-level)** — independent V2 oracle re-derives the spec, imports nothing from the engine, compared with zero tolerance. Frozen-snapshot; Mode A + Mode B; both Clover modifier representations. |
| Load / concurrency | **PROVEN** — 2,000-order / 8-wave / 3-terminal sim through the real `claim_batch`→ingest→deplete path ties out to the oracle. `MATERIALIZED` in `claim_batch` is load-bearing (prevents over-claim). |
| Webhook sales | **PARTIALLY PROVEN (synthetic)** — endpoint has full test coverage; chain is exercised via seeded inbox rows. Auth model **asserted conformant** with Clover docs (Sprint-5 scan, *not* independently re-verified — Clover's API ref is JS-rendered and couldn't be fetched): a static `X-Clover-Auth` secret (compared with `hmac.compare_digest`) + one-time `verificationCode` echo — *not* HMAC body-signing. No real Clover bytes have traversed this path. |
| Connection resilience | **BUILT + OFFLINE-TESTED** — OAuth v2, encrypted refresh tokens, rate limiting, typed errors + backoff→dead-letter. Only ever run against mocks. |

Honest one-liner: *"the depletion engine is proven correct and concurrency-safe; the
Clover integration is built and offline-conformant but not yet certified against a
live merchant."* Do not say "Clover-certified" before the sandbox sale.

### The one remaining gap (founder-only): a real Clover sandbox sale
Validates simultaneously, for the first time against real Clover: OAuth round-trip,
catalog sync → `menu_items.pos_item_id`, real webhook + the `X-Clover-Auth` value,
**real order JSON shape matching our parser** (the math proof is "given correct
parsing of Clover," not "against reality"), and a real LOCKED sale → ingest → deplete
+ a refund + a token refresh.

**#1 false-green trap:** a sale whose `menu_items.recipe_version_id` is NULL (recipe
not confirmed) ingests and marks the event processed with **zero stock movement**.
Precondition before ringing the sale: the item sold has a **confirmed** recipe mapped
to an ingredient that has stock.

## Build wiring — backend vs frontend (🟢 wired · 🟡 backend-only · ❌ not built)

| Area | Status |
|---|---|
| Auth, Clover OAuth connect, catalog sync, recipe builder, modifier config/confirm, POS picker, unit picker | 🟢 fully wired |
| Opening-balance / starting stock | 🟡 `POST /items/{id}/opening-balance` tested, **no UI caller** (`par-levels.tsx` is a stub) |
| On-hand display | 🟡 `GET /items` returns `on_hand`+`par_level`; **`stock.tsx` is a placeholder** |
| Low-stock / reorder | 🟡 data in `GET /items`; no UI |
| **Entire depletion result** (incl. refund reversal) | 🟡 built + live-proven engine; **no UI surfaces it** |
| Modifier depletion | 🟡 built + tested, not live-certified, invisible |
| Movement / "why depleted" trace | ❌ no endpoint and no UI |
| Unit-conversion / batch-yield config | 🟡 engine supports it, but confirm hardcodes `storage_unit=recipe_unit`, `yield=1`; ❌ no UI field |
| PIN setup, combos/bundles, subtractive/substitution modifiers | ❌ |
| In-app cash-tender | ❌ by design — real sales ring on Clover hardware (`scripts/v7_proof/clover_cash_pay.py` is the test harness) |

### Smallest 🟡→🟢 lifts (all use endpoints that already exist)
1. **On-hand display** — replace the `stock.tsx` placeholder; `GET /items` already
   returns name + `on_hand` + `par_level`. No backend work. Makes the engine visible.
2. **Opening-balance form** — `POST opening-balance` is ready; one form (home: the
   `par-levels` stub). Precondition for #1 to show non-zero stock.
3. **Low-stock / reorder filter** — same `GET /items` data (`on_hand < par_level`);
   folds into the stock screen.

The **false-green trap to watch**: without opening-balance in the UI, on-hand starts
at 0, so depletion drives stock negative — a real product gap, not just a missing view.

## Catalog sync note (updated 2026-06-25)
`catalog_sync.py` pages `offset/limit` (BATCH_SIZE=100) up to `MAX_PAGES=50` (5,000-item
ceiling), fetch-all-then-write. Hitting the cap now **raises** (treated as an incomplete
pull) so a truncated fetch can never drive mass soft-deletes — previously it logged a
warning and returned partial, which would have deactivated everything past the cap.
