# Verification Sprint — V1 Failure-Class Matrix

Working matrix for the V1 five-sprint failure-class audit (see `verification-sprint-spec.md`).
Canonical production-failure catalog is `../../../docs/sprint-failure-catalog.md` (F1.1–F5.10);
this matrix carries the audit's **new findings, re-grades, dispositions, and executed
evidence**. Dotted `F{sprint}.{n}` catalog entries are emitted from here at the gate.

## Global census rules (spec + lessons learned)

- **would-it-fail standard; evidence-typed; superuser-only = PARTIAL by rule** — mechanically,
  before merit — for any privilege-dependent invariant (RLS / grant / role).
- **Falsely-COVERED hunt (explicit census target).** A negative property — rollback /
  atomicity / never-on-failure / dedup-on-replay — is COVERED only if a test *injects the
  failure*. A happy-path test that never triggers the failure path does **not** cover it;
  downgrade COVERED→PARTIAL on sight. Confirmed instances this sprint: **fail-9**, the **matrix
  spot-check**, and **F2.6** — a systematic class, not a coincidence.
- **Mutation proofs run on disposable git state** (throwaway commit + `git reset --hard`, or
  `git stash`) — never in-place edit-and-restore on the working tree. Disposable git state
  protects **source, not the DB**: a test that *fails* under a mutation skips its own
  cleanup-after-assert and leaks its seed (this turn: the foreign-invite mutation left
  `user_workos_test`, poisoning 8 later tests on the fixed `workos_id`). After a mutation that
  reddens a committing test, clean its **captured id set** — and before any cleanup, check
  pre-state and scope to captured ids (never "delete where it looks like residue"; the blanket
  delete that targeted all 3510 movement-tenants was saved only by an FK). TH-1 makes this
  automatic. Lessons logged: git-checkout-over-uncommitted-fix; manual edit-and-restore;
  blanket-delete-by-resemblance; mutation-failure-leaks-seed.
- **Fills wait for the gate; recordings do not.** Re-grades/dispositions are recorded
  immediately; fill tests commit as a batch at the gate.

## Sprint → F-prefix grounding (from migration headers, not the relay)

`0001` Platform (S1/F1.x) · `0002_auth_tenants` Auth/Tenancy/RLS/RBAC (S2/F2.x) ·
`0003` Ledger (S3/F3.x) · `0006` Clover (S4/F4.x) · depletion (S5/F5.x). Auth RLS *policies*
originate in `0002`; the grant matrix spans into `0007`/`0010` and depletion RLS in S5 —
findings attach where the code lives.

## Decided rows

| ID | Sprint / F | Property (invariant) | Verdict | Evidence (executed) | Disposition / Status |
|----|-----------|----------------------|---------|---------------------|----------------------|
| revoke-IDOR | S2 / F2.x | `revoke_invitation` must not delete cross-tenant | **LIVE IDOR (fixed)** | cross-tenant test fails without app-layer `tenant_id` filter under the superuser test role; mutation-proven | **SHIPPED** `0d7973c` (Part 1, app-layer filter) |
| dev-RLS-bypass | S2 / F2.2 | the running app is subject to RLS | **PARTIAL** | `reorderos rolsuper=t`; no `SET ROLE` in `app/`; GUCs set but bypassed; RLS suite proves policies *as app_user* only | **accept-with-rationale** (app-layer `tenant_id` filtering is the proven isolation) **+** restore the RLS backstop in the A/B/C+RLS-1 migration (enable/force/policy consistency). Prod-role lookup confirms the grade (non-blocking). |
| RLS-1 | S4 / `oauth_states` | every tenant table RLS-enabled (sweep) | **"(b) incompatible" REFUTED** | all-table RLS status (4 off); `ALTER … ENABLE` succeeds in a txn; consume is pre-tenant-context | **(a) enable RLS + permissive/service policy**; folds into the A/B/C migration (4th item). Enable-without-policy = the superuser-bypass trap. |
| F2.6 | S2 / F2.x | register-tenant atomic (no orphan tenant) | **COVERED → PARTIAL** | dup-slug fails at the *first* write; no between-writes failure test existed | **fill written + mutation-proven** (premature-commit → red; restore → green); awaits gate |
| F5.3 | S5 / F5.3 | depletion-specific dup ledger (`ON CONFLICT` arbiter) | label-corrected (was "F4.2-conflict") | concurrent `write_movement` test; mutation-proven; F4.2 = the *webhook-replay* dup (distinct path, already proven) | fill written; awaits gate |
| A | S2 / `invitations` | invitations has a DELETE policy | **GAP** | `pg_policies`: 0 DELETE/ALL on `invitations`; `relforcerowsecurity=t` → non-super DELETE denied (silent no-op) | A/B/C migration |
| B | S3/S4 | consistent RLS policy targeting | **INCONSISTENCY** | most tables `{public}`; `orders`/`sale_line_items` `{app_user, service_worker}` only | A/B/C migration — **posture-first** (verify intent; not uniform-for-its-own-sake) |
| C | S3 | consistent FORCE RLS | **INCONSISTENCY** | `orders`/`sale_line_items` `relforcerowsecurity=f`; the other tenant tables `t` | A/B/C migration — **posture-first** |

## Auth-surface inventory (S2 / F2.x) — first pass complete

Classified by *(invariant-kind, assertion-role)*, never the grep ratio (the `realrole=0`
signal on `test_e2e_auth` misfired — reading-order triage, not a verdict).

| File | Invariant asserted | Assertion-role | Verdict |
|------|--------------------|----------------|---------|
| `test_jwt.py` | JWT verify (crypto) | roleless | COVERED — trap N/A |
| `test_e2e_auth.py` | Bearer JWT → `get_principal` → RBAC → tenant-filter; injects wrong-tenant / manager / staff / foreign-invite | real pipeline (app = superuser) | **COVERED** — foreign-invite mutation-proven app-layer (survives the bypass: removing `WHERE tenant_id` → leak, 1 fail / 8 pass); 403s are app-layer authz (RLS cannot emit a 403) |
| `test_rbac.py` | `require_role` owner/manager/staff | stubbed principal, app-layer guard | COVERED (role) |
| `test_rls.py` | RLS row-scoping | **`app_user` via `SET ROLE`** | COVERED — the only place RLS runs as a real RLS-subject |
| `test_auth_routes.py` | register happy / dup / invalid + atomicity | real pipeline / mutation | COVERED; **F2.6 PARTIAL → fill** |
| `test_invitations.py` | create role-gates / accept / revoke own + cross | real pipeline / mutation | COVERED; **revoke-IDOR fixed** (`0d7973c`) |
| `test_phase0_engine_isolation.py` | app vs service engine + role isolation | real connections | COVERED — **cites dev-RLS-bypass** (`SELECT current_user` asserts app = `reorderos`, "superuser for local dev") |
| `test_phase0_service_worker.py` | `service_worker` role/grants exist | **superuser catalog reads** (`pg_roles`, `has_*_privilege`) | **PARTIAL-by-rule** — grant-exists ≠ role-behaves |

**Linked finding (catalog-read ≠ behavioral coverage).** `test_phase0_service_worker`'s
grant coverage is the same class as the grant side of **F3.4** (append-only). F3.4's
`test_1_1_app_user_cannot_update_movements` is the done-right template — it *behaviorally*
attempts the forbidden UPDATE as `app_user` (already mutation-proven). One real-role test
family (connect **as** `service_worker`; exercise granted ops + assert blocked from
non-granted) closes the `service_worker` gap on that template. Tracked **linked**, not
independent. *(Note: F3.4's append-only is already behaviorally covered; what's linked is the
service_worker catalog-read gap to F3.4's behavioral pattern.)*

## S3 (Inventory Ledger) — failure-class hunt (first pass)

Negative-property coverage is **mostly genuine** — each injects its failure (the opposite of
the F2.6 pattern):
- **append-only** (F3.4): `test_1_1/1_2/7_1` attempt the forbidden UPDATE/DELETE as `app_user` → behavioral; mutation-proven.
- **dup opening_balance** (F3.2): `test_1_5/4_3/7_3` INSERT a second row → assert `23505`.
- **reversal-nets** (F3.1): `test_h0_5/h0_6` (mutation-proven); **reversal-rejects-wrong-type** `test_h0_8` raises.
- **drift** (F3.3): `test_3_1/4_7/7_24/7_25` inject counted≠predicted → assert adjust/alert.
- **cadence**: `test_6_9` rejects null Mode-B cadence.

**New finding — atomicity (failure-class 3) = the F2.6-class recurrence.** `commit_receipt`
is multi-write (per line: INSERT movement + UPDATE `emits_movement_id`; then UPDATE
`commit_state`); `record_count_event` writes a count_event + (on drift) a `count_adjust`
movement. Both rest on the caller-transaction + rollback-on-exception pattern (same as
`register_tenant` — atomic by construction). But coverage is **happy-path + idempotency only**
(`test_4_9/4_10/7_20`); **no test injects a mid-commit failure** asserting that neither the
partial movements nor `commit_state='committed'` persist. → **PARTIAL** (failure-class-3 leg
untested), same class as F2.6. Disposition: **fill** with injected-failure rollback tests (the
F2.6 template); coverage, awaits gate.

## Atomicity class — cross-surface signature sweep (S2–S5)

Signature: a function doing **2+ writes in a caller-passed session** (no self-commit) →
check for an **injected-failure-between-writes** test. The headline is reassuring — the
**highest-stakes paths are genuinely proven**, the gaps are ledger-helpers and drafts.

**COVERED (injected-failure test exists):**
- **`process_line` (S5, movements + status) — MUTATION-PROVEN this turn.** The 9/10 live-money
  invariant. `test_depleted_status_and_movements_are_atomic_injected_rollback` (phase15:310)
  injects a raise in `_set_status('depleted')` *after* the movement loop, asserts 0 movements +
  line `pending` + event `failed`, with an `autospec`+`assert_called` anti-vacuousness guard.
  Would-it-fail confirmed: injecting `session.commit()` before the status write → test reddens
  (movements persist); restore → green. **Proven, not structurally assumed.**
- `confirm_recipe` (S5) — `test_no_partial_confirm_injected_late_failure` (phase4:663).
- `confirm_modifier` (S5) — `test_no_partial_confirm_injected_late_failure` (phase6:551).
- `suggest_recipe` (S5) — `test_llm_failure_returns_503_and_writes_nothing` (phase5:314).
- catalog_sync partial-pull (S4) — `test_partial_pull_does_not_soft_delete` (phase2:167).
- worker crash-mid-line (S4) — `test_crash_mid_line_survives_and_recovers` (phase12:115).

**PARTIAL (F2.6-class — multi-write, atomic-by-construction, no injected-failure test):**
- `register_tenant` (S2/F2.6) — fill written.
- `commit_receipt` (S3) — confirmed no injected-failure test.
- `record_count_event` (S3) — confirmed no injected-failure test.
- `record_opening_balance` (S3, 2 writes) — candidate, unverified.
- `save_draft` / `skip_recipe` / `unconfirm_recipe` / `unconfirm_modifier` (S5 drafts) —
  candidates, lower-stakes (the high-stakes *confirm* paths are COVERED above).

**Disposition:** the PARTIAL set is a **REQUIRED fill** (one F2.6-template pass at the gate) —
NOT optional-because-low-severity. "Atomic by construction" is a *structural* claim that needs
the injected-failure test, because construction changes silently under refactor: `process_line`
was "atomic by construction, only the test missing" until its test existed — and it was the one
that mattered. Low *priority* (no live bug today; atomic by the caller-transaction) ≠ optional.
`commit_receipt` and `record_count_event` are required fills. The class is fully characterized
and inventoried; nothing else matches the signature unproven.

## TH-1 — test data accumulates in the test DB (test-hygiene)

**Finding:** the test DB holds ~3510 committed `inventory_movements` from accumulated e2e/worker
runs — commit-real-data tests key on unique tenants and never delete, so it grows per suite run
(surfaced during the S5 mutation cleanup). Not a production failure class; a hygiene defect that
bloats the DB and makes "is this row residue?" ambiguous — the condition behind this turn's
reckless-cleanup near-miss.

**The tests STAY** — they found the IDOR, the atomicity gaps, the RLS bypass; deleting them at
launch = shipping blind. The fix is the test *data* self-cleaning, structurally:
- **Most tests** use per-test transaction rollback (exercise everything, persist nothing — DB
  byte-identical before/after). Extend that default.
- **The few that MUST commit** (worker crash-recovery — rollback would defeat the test) get an
  explicit cleanup fixture deleting their **captured id set** (the logged lesson: scope to captured
  ids, never "delete where it looks like residue").

**Four DB lifecycles (rule 1: tests never touch production):** (1) **test** — ephemeral,
self-cleaning; (2) **dev/demo** — resettable (`dropdb`/`createdb` → `upgrade head` → seed);
(3) **production** — clean at launch, migrated forward only, never seeded, never wiped;
(4) "functionable in production" is guaranteed by production **never having had test data**, not by
scrubbing it. **MIG-1** (schema builds `0001→head` clean) is the proof the clean launch works.

**Disposition: DONE** (`3da90ef`). Per-test captured-id autoclean + session-scoped leak
detector (asserts every base table returns to pre-suite counts). Proven: **delta-0 across 10×
full suite** (594 passed each). One-time cruft cleaned preserving the reference seed.

**Findings surfaced by TH-1 (cruft was masking both):**
- **Finding 1 — FLAKY-1 (FIXED, `1dc70ea`).** `test_cross_tenant_same_order_id` processed only
  `claimed[0]`; the production worker `run()` loop processes *every* claimed row. When
  `claim_batch` returned >1, the test dropped an event → `{1000, None}`. Fixed: process all of
  the test's claimed events (matching production) + per-merchant respx mocks (total bound to
  tenant, not call order). 10× green.
- **Finding 2 — F4-claim-bound: FIXED & VERIFIED (`8c924f4`), MEDIUM, production-relevant.**
  (History: I first mis-called it "benign", then "test-harness artifact / production-safe" —
  **both retracted**; NullPool falsified the cross-loop hypothesis, and the real mechanism was
  proven by EXPLAIN. The mis-calls are kept here as the cautionary record.)
  `claim_batch(batch_size=1)` deterministically returned >1 (all pending) in a "bad" pytest
  process — instrumentation inside `claim_batch` (single `UPDATE…RETURNING` row count) fired
  **40/40**. Real at the execution level (not a DIAG illusion).
  - **FIX (shipped `8c924f4`):** wrap the locking subquery in `WITH claimed AS MATERIALIZED (…)`
    → single evaluation, plan-independent. **Verified both legs:** over-claim **0/40 across 10**
    bad-state-reproduction runs (was bimodal 40/40); **claim-once asserted** (claimed `inbox_id`s
    always distinct); fixed-query `EXPLAIN` shows the `LockRows`/`Limit` at **`loops=1`** inside a
    single CTE scan (the actual `MATERIALIZED` guarantee, not just "passed where reproducible").
    Full suite 594 green.
  - **What I got WRONG:** I claimed the cause was *cross-loop asyncpg connection reuse* and
    therefore *test-harness-only / production-safe*. **NullPool falsified that** — with NullPool
    confirmed active on both test engines (fresh connection per session, no reuse), the
    over-claim **still fired 40/40** (run 3 of 8). So the mechanism is **not** connection reuse,
    and the production-safe conclusion was unsupported. Retracted.
  - **What is established:** bimodal per-process (`0,0,40,0,0…`), in-suite-only (probe-alone
    fresh process = `0/40` ×5; needs prior tests e.g. phase6), deterministic *within* a bad
    process, **survives single-loop + fresh connections**. Single-loop *clean-state* trials are
    correct (raw `1×10`, fresh-worker `0/40`, isolation `0/70`) — but those never reproduced the
    suite-accumulated state, so they do **not** establish production safety.
  - **Mechanism: PROVEN via `EXPLAIN (ANALYZE, BUFFERS)` in the bad state.** The
    `WHERE inbox_id IN (SELECT … LIMIT 1 FOR UPDATE SKIP LOCKED)` is planned as a
    **`Nested Loop Semi Join`**: the `LIMIT 1`/`LockRows` subquery is the **inner relation,
    re-executed once per outer candidate row** (`loops=2`). Each re-execution honors `LIMIT 1`
    locally (`rows=1`) but `SKIP LOCKED` skips the already-locked row, so it locks a *different*
    one — 2 distinct rows match the semi-join → `Update … actual rows=2` for a `LIMIT 1` claim.
    Plan-shape, not stats-drift (the `Limit` node's own estimate is correct at 1). The
    documented `IN (SELECT … LIMIT … FOR UPDATE SKIP LOCKED)` nested-loop hazard.
  - **Production-relevant: YES.** The nested-loop semi-join is a normal planner choice for the
    identical production query (cost sits at the flip threshold) — not a pytest artifact. TH-1's
    cruft-clear surfaced it; it was always reachable.
  - **Severity: MEDIUM.** Violated bound = **batch-size** (a `LIMIT N` claim can return >N
    *distinct* events), **NOT claim-once** — each event still claimed once; `SKIP LOCKED` still
    prevents two workers grabbing the same row. Downstream idempotency/isolation **holds**.
    Harm: unbounded batch size + more in-flight `processing` claims (worse blast radius on a
    mid-batch crash). FLAKY-1 already makes the test robust; production still needs the fix.
  - **Fix:** `WITH cte AS MATERIALIZED (SELECT … LIMIT n FOR UPDATE SKIP LOCKED) UPDATE …
    WHERE inbox_id IN (SELECT inbox_id FROM cte)` — `MATERIALIZED` forces single evaluation of
    the locking subquery → batch bound honored under any plan. **Add to gate batch (TH-2 slot).**

**Lesson:** the one-time cruft clean must be the four-lifecycle reset (`drop/create/upgrade`,
re-seeds) or a DELETE preserving `tenant_id IS NULL` reference rows — raw `TRUNCATE` wiped the
`0014` global `unit_conversions` seed and broke 5 conversions tests.

## S1 (Platform) — failure-class sweep (fail-9 / class-7)

- **F1.1 health/deploy** — **COVERED.** `test_health.py`: `/health/live` never touches the DB;
  `/health/ready` reports degraded with an **injected** DB failure (`patch ping_database` raises).
- **F1.2 migration apply** — **COVERED.** `test_phase1_connections::test_migration_applies_and_table_exists`
  + conftest `validate_schema` (fails loudly if not at head).
- **MIG-1 migration round-trip (`0001→head→base→head`)** — **GAP.** Only forward-apply tested; no
  downgrade/round-trip → fail-9 **rollback leg untested** (same apply-but-not-rollback shape as the
  atomicity class). Also the proof-of-clean-production-launch. **Fill.**
- **F1.3 config fail-closed (class-7)** — **PARTIAL.** `test_phase0_config` only checks secrets are
  *present in the test env* and **documents** that missing `token_encryption_key` /
  `clover_webhook_auth_code` / WorkOS fields default `None`, boot **silently**, fail late. No test
  asserts production boot **fails closed**. **Fill** — prod fail-closed guard (`app_env=='production'`)
  + test.
- **CORS posture** — sub-item, not yet swept.

## Gate batch (fill together, one pass)

revoke-IDOR (✅ shipped `0d7973c`) · **TH-1** test-data self-cleaning (✅ shipped `3da90ef`, the
clean-infra foundation) + **FLAKY-1** (✅ `1dc70ea`) · atomicity class (F2.6 done; +
`commit_receipt`, `record_count_event`, opening_balance/draft candidates) · **F5.3** depletion
arbiter · **MIG-1** round-trip · **F1.3** config fail-closed · **A/B/C + RLS-1** RLS-consistency
migration (posture-first). Prod-role lookup confirms F2.2 grade (non-blocking).

**F4-claim-bound — FIXED `8c924f4` + VERIFIED (MEDIUM, production-relevant).**
`claim_batch`'s `IN (SELECT … LIMIT 1 FOR UPDATE SKIP LOCKED)` is planned as a Nested Loop Semi
Join that re-executes the locking subquery per outer row (`loops=2`) → `LIMIT N` claim returns >N
distinct events. Bound violated = **batch-size, NOT claim-once** (each event claimed once; SKIP
LOCKED holds) → downstream idempotency intact; severity MEDIUM. **Fix:** `MATERIALIZED` CTE for
the locking subquery (single evaluation, plan-independent). **Add to gate batch.** Details above.

## Pending (not decided)

- **F2.2 runtime grade — INFERRED superuser, lookup-pending (non-blocking).** The audit
  proceeds on the inference: Finding A (invitations forced + no DELETE policy → revoke works
  only via superuser bypass) already implies a superuser deployment, and Part 1 closed the
  live hole regardless of role. `test_phase0_engine_isolation` confirms app = `reorderos`
  (superuser) in dev. Pre-computed posture: app-layer `tenant_id` filtering is the actual
  isolation (comprehensive, all four e2e blocks proven app-layer); RLS is a bypassed backstop.
  The prod `DATABASE_URL` role + `is_superuser`/`bypasses_rls` + policy-target **confirm** the
  grade later; they do not gate the inventory.

## A/B/C + RLS-1 migration (deferred, posture-first)

One `invitations`/RLS-consistency migration, **after** establishing the intended RLS posture
(likely all-forced, `{public}`-targeted) and verifying `service_worker` write paths don't break:

1. **A** — add a DELETE policy on `invitations`.
2. **B** — reconcile policy targeting toward the verified-correct pattern.
3. **C** — FORCE consistency on `orders`/`sale_line_items`.
4. **RLS-1** — enable RLS on `oauth_states` + a permissive/service policy for the pre-context consume.
