# Sprint 5 — Phase Map (build sequence)

> **Companion to** `sprint-5-unified-spec-v5-LOCKED.md` (the canonical spec). This file is the
> **build sequence**: what gets built in what order, which v5 hard-exit-gates / fail-gates each phase
> closes (its deployment proof), and the deploy-gate rules. The spec defines *what*; this defines
> *the order and the proof*. Where they touch, the LOCKED spec wins.
>
> Incorporates founder edits 1–8 (2026-06-01). Branch: `sprint-5-recipe-depletion`.

## Deploy-gate rules

- **Round-trip before main.** No Phase 0 migration commit reaches `main` until `alembic upgrade head && alembic downgrade base` round-trips clean on a local DB. DDL *drafting and review* are **not** blocked on Docker; only the round-trip + commit are.
- **Phases 9 + 10 ship in ONE deploy gate (edit 2).** Phase 9 flips `depletion_status pending→depleted` after base depletion. If modifier depletion (Phase 10) isn't live, any sale *with modifiers* is marked `depleted` while missing modifier deltas → silently wrong COGS, no operator-visible error, compounding. They are one feature split for dev convenience, not two shippable units. Nothing in the depletion behavioral rewrite (Phases 8–12) reaches prod until base **and** modifier depletion are both complete.
- **Two-commit reorg (v5 §9).** Phase 7 (pure refactor, identical behavior) and Phase 9 (behavioral rewrite) are separate commits with a passing test run in between.
- **Per-phase proof.** Each phase is "deploy-ready" only when the listed v5 gates are green + ruff/mypy clean + the v5 §708 proofreading note answered.

## Phase table

| Phase | Builds | Closes (v5) | Proof |
|---|---|---|---|
| **0a** | Migration **0014** — all new tables **with inline NOT NULL/CHECK + RLS ENABLE/FORCE + tenant policies**: `recipes`, `recipe_drafts`, `recipe_llm_suggestions`, `modifiers`, `modifier_versions`, `modifier_ingredients`, `modifier_drafts`, `modifier_llm_suggestions`, `sale_line_item_modifiers`, `unit_conversions` (+ seeds). Naming per **edit 1** (`modifiers`, not `pos_modifiers`). Metadata on empty tables → batchable (std §2.1). | §6 | schema asserts; round-trip |
| **0b** | Migration **0015** — nullable cols on existing: `recipe_versions`(recipe_id, version_number, yield_quantity), `recipe_ingredients`(unit), `sale_line_items`(depletion_status, depletion_reason). `is_refunded` already exists (0006) → skip. Metadata. | §6 | round-trip |
| **0c** | Migration **0016** — **sole data-validating migration, isolated (std §4.1)**: NOT NULL + CHECK + UNIQUE tighten on the 3 pre-existing tables, with **§3 preflight (count-and-stop)** + **§1.2 five-dimension risk block**. | §6, 28, 34 | preflight passes (0 rows); round-trip |
| **0d** | Migration **0017** — `vw_depletion_coverage` view + `service_worker` UPDATE grant on `sale_line_items` + **`menu_items` service_worker INSERT/UPDATE grant + policy (Phase 2 prereq)**. Metadata. | §6, 30 | view returns both pct cols |
| **1** | Unit service `depletion/conversions.py` + `depletion/units.py` (canonical allowlist); `convert()` reads `unit_conversions` with **3-tier precedence resolution: item → tenant → global, most-specific wins** (per the tiered table fix). | §6 | gate 36 + conversion + precedence tests |
| **2** | **Clover catalog sync** — extend client (items/categories/modifier_groups); async sync at OAuth callback; `POST /pos/clover/sync-menu`; populate `menu_items` + `modifiers`(status=draft); `is_active=false` on removal; partial-unique dedup. **BLOCKED until `0017` adds `menu_items` service_worker INSERT/UPDATE grant + policy.** | §4 | gates 2, 13, 37, 42 |
| **3** | Recipe CRUD + state machine: `GET /onboarding/recipes`, GET/PATCH (**409 if confirmed**), skip, `GET /onboarding/progress`; Manager+ write, Staff read; field-blur auto-save to `recipe_drafts`. | §5 | gates 3, 11 |
| **4** | Confirm/un-confirm engine: 6-step atomic confirm (**400 if zero ingredients**, inventory auto-create case-insensitive Mode-A); 3-step atomic un-confirm; `recipe_versions` immutable. | §7, §8, §1 | gates 5,6,8,9,10,41; fail 7,8,11 |
| **5** | **LLM suggestion service** *(moved earlier — edit 7)*: `POST /onboarding/recipes/suggest`, Claude tool-use, bundled base+modifiers, confidence, **canonical-unit validation on output**, cost logging, append-only suggestion tables. **Import-isolated from `depletion/`.** | §2 | gate 7; fail-1 boundary |
| **6** | **Modifier config + confirm** *(moved later — edit 7)*: modifier PATCH/confirm (atomic, ≥1 ingredient, **additive only**), pre-populated by Phase 5 suggestions. | §3, §7 | gate 13 |
| **7** | **Depletion reorg — Commit A (pure refactor)**: `_emit_inventory_effects`→`handler.py`; `record_sale_inventory_effect`/`record_sale_reversal`→`writer.py`; stub `walker.py`/`resolver.py`; repoint 3 workers; tests identical before/after. | §9 | gates 31, 32; fail 16 |
| **8** | **Resolver** (eligibility). Predicate **phased per edit 4** (see below). CREDITED/REFUNDED rejected always. | §11 | gates 19, 22 (20 after P11) |
| **9** | **Walker + Writer — Commit B (behavioral)** *(ships with Phase 10)*: new key `sale_line:{sli}:base:{rv}:{ii}` **+ legacy-key guard (edit 3)**; formulas w/ `yield_quantity`; unit convert; Mode A/B; `yield_factor_applied` snapshot; status set in same txn; **update accounting §6 (gate 38)**. | §11 | gates 14,17,18,24,27,28,**38**; fail 2,3,4,9 |
| **10** | **Modifier depletion** *(ships with Phase 9)*: walker applies `sale_line_item_modifiers`; key `sale_line:{sli}:modifier:{slim_id}:{mv}:{ii}`; "Extra shot ×2" → 2×. Worker writes `sale_line_item_modifiers` rows **only for confirmed additive modifiers (edit 6)**. | §11 | gates 15, 16, 26 |
| **11** | **Refund/reversal wiring** + activates full PARTIALLY_REFUNDED eligibility (edit 4). Reversal scope per **edit 5**; both refund-arrival paths tested per **edit 8**. | §11 | gates 20, 21; fail 15 |
| **12** | Worker end-to-end: pending lifecycle, line-level granularity. | §11, §12 | gates 23,25,29,40; fail 13 |
| **13** | Coverage view verification + monitoring diagnostics (F5.x). Coverage *card* deferred to Sprint 9 (F.5). | §12 | gate 30 |
| **14** | **CI depletion-isolation guard** `tools/ci/check_no_llm_in_depletion.py` (direct + transitive import graph) — fails on LLM imports **and on any import/query of `recipe_drafts`/`modifier_drafts` from `depletion/`** (decision 4). | §10 | gate 33; fail 1 |
| **15** | Exit-gate sweep + fixture suite (§39) + e2e (§40). | all | gates 39, 40 |
| **16** | **Frontend (Appendix F)**: onboarding `recipes.tsx`, post-onboarding `recipes/[menuItemId]`, components, `api/recipes.ts`, EN/FR. | §1,§3,§13 | FE-1…FE-9 |

## Edit-specific rules (load-bearing detail)

**Edit 3 — legacy-key guard (Phase 9).** Before writing a new-format base movement, the writer checks for an existing movement under **both**:
```
sale_line:{sli}:base:{rv}:{ii}   (new)
sale_line:{sli}:{ii}             (legacy)
```
If either exists → skip (idempotent). Prod has 0 legacy rows today; the guard is one cheap lookup that defends dev environments and any future scenario where legacy rows exist. Retained against silent double-depletion.

**Edit 4 — phased eligibility (Phase 8 → Phase 11).** `is_refunded` is only trustworthy once Phase 11 wires line-level refund detection.
- **Before Phase 11 ships:** `PARTIALLY_REFUNDED` is **non-eligible** (treated like `REFUNDED`/`CREDITED`).
- **After Phase 11 ships:** full predicate active:
  ```
  orders.state = 'locked'
  AND orders.payment_state IN ('PAID','PARTIALLY_REFUNDED')
  AND sale_line_items.is_voided = false
  AND sale_line_items.is_refunded = false
  ```
- `CREDITED` and `REFUNDED` rejected in **both** phases.

**Edit 5 — reversal scope (Phase 11).** `record_sale_reversal` reverses **all** prior movements for a sale line:
- base movements **and** modifier movements
- new-format keys **and** legacy-format keys
- idempotently
- **if no forward movement exists → no-op** (handles refund-before-depletion cleanly, no errors)

**Edit 6 — `sale_line_item_modifiers` write rule (Phase 10).** Rows are written **only** for depletion-relevant modifiers: `modifier_version_id NOT NULL`; the worker **skips** unconfirmed modifiers (status != 'confirmed') and non-additive modifiers (subtractive/substitution). Full POS-modifier audit logging is a future sprint, not Sprint 5.

**Edit 8 — both refund paths fixture-tested (Phase 11).**
- *Refund after depletion:* triggers `record_sale_reversal()` (edit 5) over base + modifier movements.
- *Refund before depletion:* marks `is_refunded=true` first; when depletion runs, eligibility fails → `depletion_status='failed'` / `reason='line_refunded'`, no ledger writes.
Both require fixtures; the second is the easy-to-forget case.

## DDL-review rules (round 2 — 0014)

**`unit_conversions` is three-tier, not global-only.** A composite `PK(from_unit,to_unit)` is forbidden — it structurally blocks per-ingredient density (v5 §6: weight→volume needs a non-NULL `inventory_item_id`; `cup` differs by ingredient). Shape: surrogate `id` PK + nullable `tenant_id`/`inventory_item_id` + `dimension`, three partial-unique indexes (global / tenant / item), RLS `tenant_id IS NULL OR tenant_id = current_tenant`. Global tier seeded; tenant/item override writes are a later sprint. **Phase 1 walker must resolve precedence item → tenant → global (most-specific wins).**

**`modifiers.current_version_id` integrity (decision 3) = DB-level composite FK.** `(modifiers.id, current_version_id) → modifier_versions(modifier_id, id)` (backed by `UNIQUE(modifier_id, id)` on `modifier_versions`). A version can never be pointed at by the wrong modifier. NULL while draft/skipped (MATCH SIMPLE skips the check).

**Depletion never reads drafts (decision 4) = fail-gate invariant.** `recipe_drafts`/`modifier_drafts` are mutable scratch; confirm materializes them into immutable `*_versions`; depletion reads only `*_versions`. Enforced by the Phase 14 guard (extended above).

**`sale_line_item_modifiers` is a subset, not an audit (edit 6).** Worker writes rows only for confirmed additive modifiers; unconfirmed/non-additive are intentionally skipped (no row). Documented in the migration so no future dev reads it as a complete modifier record.

## Approved (locked — not re-litigated)

- Migration split posture: inline NOT NULL/CHECK/RLS in 0014 (metadata on empty tables, std §2.1); isolated tightening in 0016 with §3 preflight + §1.2 risk block (std §4.1).
- Catalog sync placement before Recipe CRUD; async-from-OAuth-callback design.
- LLM suggestion import-isolation from `depletion/`; write-only to draft tables; operator confirm = only promotion path.
- Pure refactor (Phase 7) before behavioral rewrite (Phase 9).
- Per-phase proof tied to v5 gate numbers.
