# ReorderOS build-status audit — backend vs frontend wiring

_Date: 2026-06-25. Branch `sprint-5-recipe-depletion`. Method: grepped every API path the
frontend actually calls + read each screen; placeholders are explicit `Stub`/`TabPlaceholder`
imports, not inference._

Legend: ❌ not built · 🟡 backend built + tested but NOT wired to frontend · 🟢 fully wired
(a user can do it in the UI). **🟢 requires real frontend that calls the endpoint — an endpoint
existing is not 🟢.**

> Headline: the backend value-loop is built and **live-certified on real Clover** (V7), but
> almost none of its *output* is visible in the app. Depletion runs correctly and invisibly.

## POS / Clover
| Capability | Status | Evidence |
|---|---|---|
| OAuth connect (click → token stored) | 🟢 | `connecting.tsx` opens WebBrowser auth via `connect-url`; `callback.tsx` returns; status in `more.tsx`/`found-summary.tsx` |
| Catalog/menu sync | 🟢 | auto-kicks at OAuth callback; status visible; synced `menu_items` consumed by the recipe builder |
| Webhook → inbox → depletion | 🟡 | live-certified; server-to-server (no UI for the webhook, correctly) — but the **result** surfaces nowhere |
| Cash-tender + lock | ❌ (script-only) | `scripts/clover_cash_pay.py`; by design NOT a ReorderOS UI feature — real sales ring on Clover hardware |

## Onboarding
| Capability | Status | Evidence |
|---|---|---|
| Recipe builder (create/edit/confirm) | 🟢 | `recipes.tsx` — patch/confirm/unconfirm, field-blur auto-save |
| Opening-balance / starting stock | 🟡 | `POST /items/{id}/opening-balance` exists+tested; **no frontend caller** (API-only). `par-levels.tsx` is a Stub |
| Unit-conversion + batch-yield fields | 🟡 backend / ❌ UI | engine supports+tested, but confirm hardcodes `storage_unit=recipe_unit`,`yield=1`; no UI field |
| PIN setup | ❌ | `pin.tsx` is a Stub |
| POS selection | 🟢 | `pos-picker.tsx` → wired connect flow |

## Inventory
| Capability | Status | Evidence |
|---|---|---|
| On-hand display | 🟡 | `GET /items` computes `on_hand`+`par_level`; **`stock.tsx` is a `TabPlaceholder`** |
| Movement history / "why depleted" trace | ❌ | no read endpoint AND no UI |
| Low-stock / reorder views | 🟡 | data in `GET /items`; no UI consumes it |

## Depletion engine — visible to a user?
single / multi-line / qty>1 / 20-ingredient / conversion / refund / void: **engine ✅ proven**
(tests; single live-certified) — **user-visible: NO**, none of it (stock + history are placeholders).

## Modifiers / combos
- Modifiers: config/confirm **🟢** (`ModifierSubsection` in `recipes.tsx` + API calls); depletion **🟡**
  (built+tested, not live-certified, invisible); subtractive/substitution **❌** (out of v5 scope).
- Combos/bundles: **❌** — neither layer.

## Three lists
**(1) Fully wired 🟢:** auth/sign-in, Clover OAuth connect, catalog sync, recipe builder, modifier
config/confirm, POS picker, canonical unit picker.

**(2) Backend done, NOT reachable from UI 🟡 — "looks done but isn't":**
- Opening-balance / starting stock (API-only)
- On-hand display (endpoint ready, screen is a placeholder)
- Low-stock / reorder (data in `GET /items`, no view)
- **The entire depletion result** (webhook→deplete + refund-reversal work + live-proven; no UI)
- Modifier depletion (built, not live-certified, invisible)

**(3) Not built ❌:** movement/trace history (no endpoint or UI), unit-conversion/batch-yield config,
PIN setup, combos, in-app cash-tender (script-only by design), stubbed onboarding steps
(par-levels, team, suppliers, biometric, manual-menu, billing, cleanup, done).

## Backend/frontend disagreements (API supports it, UI silently can't)
- **Opening-balance:** API ✔ / UI ✗ → can't set starting stock → on-hand starts at 0 → depletion
  goes negative (the "false-green" trap, now a product gap).
- **`GET /items`:** built ✔ / no frontend caller ✗.
- **Depletion + refund reversal:** backend ✔ / UI ✗ → core value invisible.
- **Conversion/yield:** engine ✔ / UI ✗ + confirm hardcodes identity → double-gated.

## Smallest 🟡→🟢 lifts (all lean on endpoints that ALREADY exist)
1. **On-hand display** — `GET /items` already returns name+`on_hand`+`par_level`. Lift = one screen
   (replace `stock.tsx` placeholder; port legacy StockTab). No backend work. Makes the engine visible.
2. **Opening-balance form** — `POST opening-balance` ready; one form (the `par-levels` stub is the home).
   Precondition for #1 to show non-zero stock.
3. **Low-stock / reorder filter** — same `GET /items` data (`on_hand < par_level`); folds into the stock screen.

Bigger (not quick wins): movement/trace = new endpoint; conversion/batch-yield = adaptive-onboarding
design; combos = full sprint.
