# Sprint 5 — Phase 15 notes: exit-gate sweep (traceability matrix)

> The exit-gate sweep is an AUDIT, not a rewrite. This matrix maps every hard-exit-gate (1–42),
> fail-gate (1–16), and §39 fixture item to the test that proves it. A gate is "COVERED" only if
> its cited test would **fail** when the property breaks — a citation that merely *touches the
> area* is marked PARTIAL. This matrix is Claude auditing Claude; it is reviewed before any
> gap-filling test is written, because a green checkmark becomes the permanent record.
>
> **Type** (what level the citation proves): `U` pure unit · `I` DB integration · `E` real-role
> e2e (worker/service_worker, webhook→ledger) · `S` static guard · `M` migration/preflight ·
> `D` diagnostic/monitor · `P` process/git · `DOC` document.
>
> **Rule (type has teeth):** a gate that claims an end-to-end property ("produces ledger
> movements", "the worker", anything spanning webhook→ledger) cited only against `U`/`I` is
> **PARTIAL by rule** — the missing level is named.
>
> **Status:** COVERED · PARTIAL · GAP. **Disposition** (partial/gap only): `fill-now` ·
> `accept` (+ why) · `tracked` (owned by a later phase).

## Hard exit gates — configuration path (1–13)

| # | Property | Citation | Type | Status | Disposition |
|---|---|---|---|---|---|
| 1 | Complete Recipes step, ≥1 confirmed | `phase4::test_confirm_happy_creates_version_and_links` | I | COVERED | |
| 2 | Menu sync at OAuth callback + `/sync-menu` | `phase2::test_sync_menu_manager_202`, `::test_sync_populates_menu_items_and_modifiers` | I | COVERED | callback invokes the same `CatalogSyncService` the test exercises |
| 3 | `GET /onboarding/recipes` full list | `phase3::test_list_shows_menu_items_state_none` | I | COVERED | |
| 4 | Draft auto-save | `phase3::test_patch_creates_recipe_and_draft_only`, `::test_patch_twice_updates_single_draft` | I | COVERED | |
| 5 | Confirm/un-confirm/skip transitions | `phase4::test_confirm_happy*`, `::test_unconfirm_copies*`, `phase3::test_skip_preserves_draft_and_patch_reopens` | I | COVERED | |
| 6 | Un-confirm preserves versions immutable + atomic | `phase4::test_unconfirm_copies_draft_and_keeps_version_immutable` | I | COVERED | |
| 7 | LLM base+modifiers in one call | `phase5::test_suggest_stores_base_and_modifier_appendonly` | I | COVERED | |
| 8 | Inventory auto-create, case-insensitive dedup | `phase4::test_confirm_dedups_inventory_item_case_insensitively` | I | COVERED | |
| 9 | Confirm atomic across 6 steps | `phase4::test_no_partial_confirm_injected_late_failure`, `::test_no_partial_confirm_unit_type_conflict` (real-txn) | I | COVERED | |
| 10 | Confirm 400 if zero ingredients | `phase4::test_confirm_no_draft_400`; `phase3::test_patch_validation_400`; **`phase15::test_confirm_zero_ingredient_draft_raises_emptydraft`** (deep branch) | I | COVERED | (was PARTIAL → filled) |
| 11 | PATCH on confirmed → 409 | `phase3::test_patch_409_when_confirmed`, `phase4::test_patch_409_after_real_confirm` | I | COVERED | |
| 12 | Non-canonical unit → 400 at API | `phase3::test_patch_validation_400` | I | COVERED | |
| 13 | Modifier sync populates `modifiers` + `sale_line_item_modifiers` | `phase2::test_sync_populates*` (modifiers); `e2e_10` (slim rows, worker-written) | I/E | COVERED | slim rows are worker-written at ingestion, not by catalog sync — both halves cited |

## Hard exit gates — depletion path (14–30)

| # | Property | Citation | Type | Status | Disposition |
|---|---|---|---|---|---|
| 14 | Eligible sale → correct movements | `e2e_1_mode_a_sale_writes_depletion` | E | COVERED | basic path here; **formula-value correctness** (factor≠1, yield division) cited at `phase9::test_storage_to_recipe_factor_is_not_consulted`/`test_yield_quantity_divides` + `e2e_2_delta_formula` — coverage is distributed, not missing |
| 15 | Modifier → additional movement, distinct key | `e2e_10_confirmed_modifier_depletes`, `phase10::test_shared_item_base_and_modifier_distinct_rows_both_apply` | E/I | COVERED | |
| 16 | Multiplier ×2 → 2× depletion | `phase10::test_modifier_multiplier_applied`, `e2e_10` (qty 2 → ×2) | I/E | COVERED | |
| 17 | Mode A → `sale_depletion` negative | `phase9::test_mode_a_depletes_negative`, `e2e_1` | I/E | COVERED | |
| 18 | Mode B → `sale_signal` positive | `phase9::test_mode_b_signals_positive`, `e2e_3_mode_b_writes_signal` | I/E | COVERED | |
| 19 | **CREDITED → no forward depletion** (v4 regression) | `phase8::test_credited_not_eligible_v4_regression` (U); `phase9::test_ineligible_fails_no_rows[CREDITED]` (I); **`phase15::test_e2e_credited_order_no_forward_depletion`** (E) | U/I/E | COVERED | (was PARTIAL → filled with webhook→ledger e2e) |
| 20 | PARTIALLY_REFUNDED: non-refunded deplete, refunded → line_refunded | `phase11::test_e2e_partially_refunded_mixed_lines` | E | COVERED | |
| 21 | Line refund → `record_sale_reversal` | `phase11::test_e2e_refund_after_depletion_reverses_real_role` | E | COVERED | partial-refund variant cited at gate 20; **`source_id` audit linkage** asserted at `phase11::test_e2e_reversal_is_additive_forward_rows_untouched` (`rev.source_id == forward.id`) |
| 22 | Voided line → `failed`/`sale_ineligible` | `phase8::test_voided_line_ineligible` (U); `e2e_7` (E, no movement); **`phase15::test_e2e_voided_line_row_is_failed_sale_ineligible`** (E, row status) | U/E | COVERED | (was PARTIAL → filled the row-status assertion the parametrized test missed by never varying `is_voided`) |
| 23 | Line-level failure granularity | `phase12::test_line_failure_isolation_graceful`, `::test_crash_mid_line_survives_and_recovers` | E | COVERED | |
| 24 | Duplicate events → no duplicate rows | `e2e_5_replay_idempotent`, `phase9::test_replay_is_idempotent`, `phase7::test_replay_no_duplicate` | E/I | COVERED | |
| 25 | Recipe edits after processing don't alter ledger | `phase12::test_unconfirm_preserves_historical_ledger`, `e2e_4_recipe_version_frozen` | E | COVERED | |
| 26 | **Modifier edits after processing don't alter ledger** | **`phase15::test_e2e_modifier_edit_does_not_alter_processed_sale_ledger`** | E | COVERED | (was GAP → filled; real modifier un-confirm, movements+slim+version unchanged, with vacuousness precondition) |
| 27 | Unmapped/skipped/failed → status/reason, no rows | `phase9::test_null_snapshot_is_unmapped_no_recipe`, `::test_ineligible_fails_no_rows`, `e2e_8_no_recipe_no_movement` | I/E | COVERED | |
| 28 | `depletion_status_reason_consistency` CHECK holds | migration 0016 CHECK (structural); exercised by every status write | M | COVERED | structural-mechanism |
| 29 | `pending` >5 min queryable | `phase12::test_stuck_pending_lines_queryable` | D | COVERED | |
| 30 | Coverage returns both pct columns | `phase13::test_coverage_*` (3 states) | I | COVERED | |

## Hard exit gates — architecture (31–38) + test coverage (39–42)

| # | Property | Citation | Type | Status | Disposition |
|---|---|---|---|---|---|
| 31 | All depletion code under `depletion/` | `phase14` guard scans that dir; grep | S | COVERED | |
| 32 | Commit A / Commit B separate w/ intermediate pass | git: `99218cf` (A, suite 501 unchanged) / `18d6e5d` (B) | P | COVERED | process |
| 33 | CI fails on LLM import (direct/transitive) | `phase14::*` (5 sensitivity + real-tree), `phase5::test_depletion_does_not_import_llm` | S | COVERED | |
| 34 | Migration risk standard followed | every migration 0014–0021 carries §1.2 block + preflight | M | COVERED | |
| 35 | Supersession note added | `docs/archive/v1-backend-build-plan.md` header (committed pre-Sprint-5) | DOC | COVERED | verify file present |
| 36 | Canonical allowlist at API, UI, DB | API `phase3::test_patch_validation_400`; DB 0016 CHECK; **UI = Phase 16** | I/M | **PARTIAL** | **tracked (Phase 16)** — UI-layer enforcement is the frontend unit picker (FE-4) |
| 37 | Modifier uniqueness via partial-unique index | `phase2::test_resync_dedups_modifiers`, `::test_menu_items_pos_item_unique`, migration 0018 | I/M | COVERED | |
| 38 | Accounting §6 updated with new key formats | `inventory_accounting_semantics.md` §6 (base/modifier/reversal/legacy) | DOC | COVERED | |
| 39 | Fixture coverage (15 items) | see §39 checklist below | — | (meta) | |
| 40 | E2E: inbox → worker → depletion → ledger | `test_e2e_pos_inventory` (E2E.1–12) | E | COVERED | |
| 41 | Un-confirm round-trip | `phase4::test_unconfirm_copies*` (I), `phase12::test_unconfirm_preserves_historical_ledger` (E) | I/E | COVERED | |
| 42 | Menu sync catalog pull populates | `phase2::test_sync_populates_menu_items_and_modifiers` | I | COVERED | |

## Fail gates (1–16) — evidence kind: `prevention-test` (attempts Y, proves prevented) or `structural-mechanism` (makes Y impossible)

| # | Must never happen | Citation | Evidence kind | Status | Disposition |
|---|---|---|---|---|---|
| 1 | LLM in depletion path | `phase14` guard; `phase5::test_depletion_does_not_import_llm` | structural (guard) + prevention | COVERED | |
| 2 | Duplicate movements under retry | `e2e_5`, `phase9::test_replay_is_idempotent`, `phase11::test_e2e_replayed_refund_event_writes_one_reversal` + UNIQUE(tenant,idempotency_key)+ON CONFLICT | prevention + structural | COVERED | |
| 3 | Version change alters prior ledger | recipe: `phase12::test_unconfirm_preserves_historical_ledger`, `e2e_4`; modifier: `phase15::test_e2e_modifier_edit_does_not_alter_processed_sale_ledger` | prevention | COVERED | (was PARTIAL → modifier half filled, gate 26) |
| 4 | Mode A/B confuse types | `phase9::test_mode_a/mode_b`, `phase11::test_reverse_line_mode_b_reverses_signal` | prevention | COVERED | |
| 5 | UI accepts invalid unit/neg/empty | API: `phase3::test_patch_validation_400`; **UI = Phase 16** | prevention | **PARTIAL** | **tracked (Phase 16)** — FE-2/FE-4 |
| 6 | Migration violates risk standard | every 0014–0021 §1.2 block + isolation rule | structural | COVERED | |
| 7 | Un-confirm mutates a version row | `phase4::test_unconfirm_copies_draft_and_keeps_version_immutable`, `phase12` byte-identical asserts | prevention | COVERED | |
| 8 | PATCH succeeds on confirmed | `phase3::test_patch_409_when_confirmed`, `phase4::test_patch_409_after_real_confirm` | prevention | COVERED | |
| 9 | `depleted` with no movements | structural (movements + `status='depleted'` in ONE worker txn) + **`phase15::test_depleted_status_and_movements_are_atomic_injected_rollback`** (injected-rollback, autospec+assert_called) | structural + prevention | COVERED | (was structural-only → added the injected-failure test per the fail-gate evidence bar) |
| 10 | Non-canonical unit in columns | 0016 CHECK on recipe_ingredients/modifier_ingredients/unit_conversions; `phase1::test_canonical_allowlist` | structural | COVERED | |
| 11 | Confirm with zero ingredients | `phase4::test_confirm_no_draft_400`; `phase15::test_confirm_zero_ingredient_draft_raises_emptydraft` | prevention | COVERED | (was PARTIAL → filled, gate 10) |
| 12 | Duplicate POS modifier rows | 0018 partial-unique; `phase2::test_resync_dedups_modifiers` | structural + prevention | COVERED | |
| 13 | Sale-level failure blocks lines | `phase12::test_line_failure_isolation_graceful`, `::test_crash_mid_line_survives_and_recovers` | prevention | COVERED | |
| 14 | CREDITED triggers forward depletion | `phase8`, `phase9[CREDITED]`, `phase15::test_e2e_credited_order_no_forward_depletion` | prevention | COVERED | (was PARTIAL → filled with e2e, gate 19) |
| 15 | Line refund → no reversal | `phase11::test_e2e_refund_after_depletion_reverses_real_role` | prevention | COVERED | |
| 16 | Commit A changes behavior | `phase7` (suite 501 before==after; import-only test diff) | process | COVERED | |

## §39 fixture checklist

| Fixture | Citation | Status |
|---|---|---|
| mapped sale | `e2e_1` | COVERED |
| unmapped sale | `e2e_8`, `phase9::test_null_snapshot_is_unmapped_no_recipe` | COVERED |
| duplicate sale | `e2e_5`, `phase9::test_replay_is_idempotent` | COVERED |
| modifier sale, multiplier > 1 | `phase10::test_modifier_multiplier_applied`, `e2e_10` | COVERED |
| both modes | `phase9::test_mode_a/mode_b`, `e2e_1`/`e2e_3` | COVERED |
| CREDITED rejection | `phase8`, `phase9[CREDITED]`, `phase15::test_e2e_credited_order_no_forward_depletion` | COVERED |
| PARTIALLY_REFUNDED partial depletion | `phase11::test_e2e_partially_refunded_mixed_lines` | COVERED |
| line refund reversal | `phase11::test_e2e_refund_after_depletion*` | COVERED |
| voided line | `e2e_7`, `phase8::test_voided_line_ineligible`, `phase15::test_e2e_voided_line_row_is_failed_sale_ineligible` | COVERED |
| recipe edit non-effect | `phase12::test_unconfirm_preserves_historical_ledger`, `e2e_4` | COVERED |
| modifier edit non-effect | `phase15::test_e2e_modifier_edit_does_not_alter_processed_sale_ledger` | COVERED |
| unit conversion correctness | `phase1::*`, `e2e_2_delta_formula` | COVERED |
| idempotency under replay | `e2e_5`, `phase9::test_replay_is_idempotent` | COVERED |
| worker crash mid-depletion | `phase12::test_crash_mid_line_survives_and_recovers` | COVERED |
| confirmation with zero ingredients (rejected) | `phase4::test_confirm_no_draft_400`, `phase15::test_confirm_zero_ingredient_draft_raises_emptydraft` | COVERED |

## Gap summary — the 5 fill-now gaps (DONE) + 2 tracked

**fill-now (5 tests in `tests/test_sprint5_phase15_exit_gates.py`) — all written and passing:**
1. **CREDITED e2e** (gates 19 + fail-14): `test_e2e_credited_order_no_forward_depletion` — CREDITED order through the real worker → **no** movements + line `failed`/`sale_ineligible`. Webhook→ledger, because the v4 bug was end-to-end.
2. **Voided line row-status** (gate 22): `test_e2e_voided_line_row_is_failed_sale_ineligible` — completes the verdict-only + no-movement coverage with the row-status assertion (the axis the parametrized ineligible test never varied — `is_voided`).
3. **Modifier edit non-effect** (gate 26 + fail-3 modifier half): `test_e2e_modifier_edit_does_not_alter_processed_sale_ledger` — real modifier un-confirm after a processed modifier sale; modifier movements + frozen slim + version byte-identical. Carries the **vacuousness precondition** (modifier movement exists keyed by modifier v1 before the edit).
4. **Confirm-time EmptyDraft** (gate 10 + fail-11): `test_confirm_zero_ingredient_draft_raises_emptydraft` — a zero-ingredient draft seeded **directly** → confirm hits the authoritative `repo.py:463` `EmptyDraft` (no version, draft intact), not the API fast-path.
5. **Fail-9 injected-rollback** (added in review): `test_depleted_status_and_movements_are_atomic_injected_rollback` — inject a raise after `write_movement`, before the `depleted` status commit; the per-line txn rolls back BOTH (no movements, line stays pending). Uses `autospec=True` + `assert_called()` so a future rename/inline of `_set_status` fails loudly instead of the injection silently never firing.

**tracked (Phase 16):** gate 36 UI-layer canonical units; fail-5 UI invalid-unit/neg/empty. These are the frontend unit picker — **added explicitly to Phase 16 scope (FE-2 zero-ingredient disable, FE-4 canonical-only unit picker)**, not left as a matrix annotation.

**accept:** none.

**Lesson (from gap 2):** when citing a **parametrized** test as coverage, check the **axis list, not the test name**. `test_ineligible_fails_no_rows` *looked* comprehensive but parametrized only `(payment_state, order_state)` — it never varied `is_voided`, so gate 22's voided row-status was uncovered behind a green-looking family. A parametrized test covers exactly the axes it parametrizes.

> Red-flag check (per review): the matrix was **not** all-green — 5 fill-now gaps + 2 Phase-16-tracked partials across 42 gates + 16 fail-gates, including a self-caught near-over-claim (gate 35, wrong path). That is the expected shape for a 15-phase build. The 5 are now closed; the 2 are owned by Phase 16.
