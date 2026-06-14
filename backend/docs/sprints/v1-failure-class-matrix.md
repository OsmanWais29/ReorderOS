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
  `git stash`) — never in-place edit-and-restore on the working tree. Two near-misses logged:
  the `git checkout`-over-uncommitted-fix slip, and the manual edit-and-restore round.
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
| dev-RLS-bypass | S2 / F2.2 | the running app is subject to RLS | **PARTIAL** | `reorderos rolsuper=t`; no `SET ROLE` in `app/`; GUCs set but bypassed; RLS suite proves policies *as app_user* only | tracked; prod-role grade pending lookup |
| RLS-1 | S4 / `oauth_states` | every tenant table RLS-enabled (sweep) | **"(b) incompatible" REFUTED** | all-table RLS status (4 off); `ALTER … ENABLE` succeeds in a txn; consume is pre-tenant-context | **(a) enable RLS + permissive/service policy**; folds into the A/B/C migration (4th item). Enable-without-policy = the superuser-bypass trap. |
| F2.6 | S2 / F2.x | register-tenant atomic (no orphan tenant) | **COVERED → PARTIAL** | dup-slug fails at the *first* write; no between-writes failure test existed | **fill written + mutation-proven** (premature-commit → red; restore → green); awaits gate |
| F5.3 | S5 / F5.3 | depletion-specific dup ledger (`ON CONFLICT` arbiter) | label-corrected (was "F4.2-conflict") | concurrent `write_movement` test; mutation-proven; F4.2 = the *webhook-replay* dup (distinct path, already proven) | fill written; awaits gate |
| A | S2 / `invitations` | invitations has a DELETE policy | **GAP** | `pg_policies`: 0 DELETE/ALL on `invitations`; `relforcerowsecurity=t` → non-super DELETE denied (silent no-op) | A/B/C migration |
| B | S3/S4 | consistent RLS policy targeting | **INCONSISTENCY** | most tables `{public}`; `orders`/`sale_line_items` `{app_user, service_worker}` only | A/B/C migration — **posture-first** (verify intent; not uniform-for-its-own-sake) |
| C | S3 | consistent FORCE RLS | **INCONSISTENCY** | `orders`/`sale_line_items` `relforcerowsecurity=f`; the other tenant tables `t` | A/B/C migration — **posture-first** |

## Pending (not decided)

- **F2.2 runtime grade** — awaiting prod `DATABASE_URL` role + `is_superuser`/`bypasses_rls` +
  policy-target confirmation. Pre-computed: app-layer `tenant_id` filtering is the actual
  isolation (comprehensive except the now-fixed revoke path); RLS is a bypassed backstop.
  Grade resolves on the lookup.

## A/B/C + RLS-1 migration (deferred, posture-first)

One `invitations`/RLS-consistency migration, **after** establishing the intended RLS posture
(likely all-forced, `{public}`-targeted) and verifying `service_worker` write paths don't break:

1. **A** — add a DELETE policy on `invitations`.
2. **B** — reconcile policy targeting toward the verified-correct pattern.
3. **C** — FORCE consistency on `orders`/`sale_line_items`.
4. **RLS-1** — enable RLS on `oauth_states` + a permissive/service policy for the pre-context consume.
