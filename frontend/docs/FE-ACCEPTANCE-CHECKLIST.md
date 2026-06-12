# Sprint 5 — Frontend acceptance checklist (Appendix F.6, FE-1…FE-9)

> **Why this file exists.** Phase 16 ships under verification posture (1b): `tsc --noEmit` +
> `expo lint` + structural review against Appendix F. The agent that built these screens
> **cannot run the Expo app**, so FE-1…FE-9 are **not** attested by it — they are executed by a
> reviewer (or a frontend-capable run) against the running app, and the result is recorded here
> as a durable artifact (per-item pass/fail), not a vibe. Until a row is checked, it is
> **unexecuted**, and the matrix keeps gate 36 / fail-5 marked PARTIAL.
>
> Run context: `cd frontend && npx expo start` (or a device/simulator build).

## Pre-flight (mechanically verifiable — already attested by the build)

- [x] **FE-0a** `tsc --noEmit` is clean (0 errors) — the whole frontend, baseline restored.
- [x] **FE-0b** backend `test_frontend_units_sync.py` passes — the unit picker offers exactly
      the backend canonical allowlist, set-equal AND dimension-grouped (gate 36 enforcement).
- [x] **FE-0c** backend `test_frontend_i18n_completeness.py` passes — EN/FR key parity (FE-8
      key-presence; translation *quality* is a human check, FE-8 below).
- [ ] **FE-0d** `expo lint` is clean — **NOT runnable in the build sandbox** (eslint not
      installed); run it in a frontend-capable environment.

## Acceptance gates (execute against the running app)

- [ ] **FE-1** `GET /onboarding/recipes` renders the accordion list with correct status badges
      (none/draft/confirmed/skipped).
- [ ] **FE-2** Confirm is disabled at zero ingredients (and on a duplicate-name or invalid row);
      a server 400 surfaces as a validation message.
- [ ] **FE-3** PATCH on a confirmed recipe → 409 surfaces as an "Edit recipe first" prompt
      (`recipes409`), branched per-call (not a global status→message map).
- [ ] **FE-4** Unit picker offers ONLY canonical units (grouped by dimension); a non-canonical
      unit cannot be selected; a server 400 on a bad unit is handled.
- [ ] **FE-5** Field-blur auto-save persists the draft; app close preserves draft state.
- [ ] **FE-6** Un-confirm ("Edit recipe") round-trips in the UI; the recipe returns to draft.
- [ ] **FE-7** "Extra shot ×2" modifier shows the multiplier; subtractive modifiers are disabled
      *(post-onboarding detail screen — deferred slice; verify when that screen lands)*.
- [ ] **FE-8** EN/FR toggle covers every recipe/modifier string with no missing keys AND the FR
      copy is correct (parity is FE-0c; this row is the human translation-quality check).
- [ ] **FE-9** Unauthenticated requests → 401 with no ghost data rendered.

## Tracked compliance debt (bilingual — Charter of the French Language / Bill 96)

- [ ] **DEBT-i18n-legacy** — the legacy onboarding screens (`account`, `connecting`,
  `pos-picker`, `found-summary`, etc.) **hardcode English**; only the new Recipes screen routes
  through `strings.ts` (EN+FR). An operator therefore experiences a flow that is **not
  bilingual end-to-end**. The French-language obligation is the **Charter of the French Language
  as amended by Bill 96** (NOT Law 25 — that is Québec's *privacy* statute; distinct). Out of
  Sprint 5 scope (not introduced by Phase 16). **Remediation trigger: BEFORE the first
  French-speaking pilot onboards** (a Montréal/Québec cohort makes this near-certain — schedule
  it against that date, not "on next touch"). Fix = migrate the legacy literals into
  `strings.ts` (EN+FR) and add those files to `test_frontend_i18n_completeness.py`'s scan list
  so the guards enforce it permanently (~1 day). Verify the Bill 96 specifics against current
  guidance when scheduling.

## Notes

- The **modifier sub-section** ships in Phase 16 **slice 3** (in Sprint-5 scope — without it no
  modifier can be confirmed, so modifier depletion has zero pilot reach). The post-onboarding
  `(app)/recipes/[menuItemId]` **detail screen** is the deferred follow-up (own section below).
- Gate 36 (canonical units, UI) and fail-5 (UI rejects invalid/neg/empty) remain **PARTIAL** in
  the Phase 15 matrix until FE-2 and FE-4 are checked here against the running app.

## Deferred follow-up (post-Sprint-5)

- [ ] Post-onboarding `(app)/recipes/index.tsx` + `[menuItemId].tsx` detail/edit screens (§13).
  Operators can edit during onboarding; founder-led support covers post-onboarding editing for
  the pilot. Track with its own FE checklist when it lands.
