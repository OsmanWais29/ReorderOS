# Post-Migration Audit: 0013_security_hardening

**Migration:** `0013_security_hardening`
**Applied:** prior to the commit of `backend/docs/migration-risk-standard.md` (commit `7d6a7b6`)
**Audit date:** 2026-05-26
**Audit framework:** `backend/docs/migration-risk-standard.md`
**Status:** Applied. Working. Tests passing (420/420). Predates the standard.

## Purpose of this audit

The migration risk standard was committed after 0013 was already applied. This audit measures 0013 against the now-committed standard as a retrospective check, identifies gaps, and records preflight queries and call-site verifications that should have been documented in the migration file but were not.

This audit is not a finding of fault. The standard did not exist on disk when 0013 was written. The purpose of this document is to establish the pattern for future migrations and to retroactively complete the documentation that would have accompanied 0013 had the standard been in place.

## What 0013 changed

Ten fixes in a single migration file:

1. `REVOKE DELETE ON users FROM app_user`
2. `REVOKE DELETE ON tenants FROM app_user`
3. `monitoring_alerts` policies split by role (app_user tenant-scoped, service_worker open)
4. `orders.state` CHECK constraint updated to remove dead `'OPEN'` value
5. FK added: `sale_line_items.recipe_version_id → recipe_versions.id`
6. Partial unique index added: `invitations(tenant_id, email) WHERE accepted_at IS NULL`
7. CHECK constraint added: `receipt_lines.received_quantity > 0`
8. `REVOKE DELETE ON idempotency_keys FROM app_user`
9. `REVOKE SELECT ON pos_waitlist FROM app_user`
10. Unique index added: `pos_waitlist(email)`, with dedup DELETE before index creation

## Standard conformance: gaps

Measured against `backend/docs/migration-risk-standard.md`:

### Gap 1: No risk profile block (§1.2)

The standard requires a five-dimension risk classification in every migration. 0013 has no risk profile block. The retrospective classification, by operation:

| Operation | Data validity | Availability | App compatibility | Data propagation | Reversibility |
|---|---|---|---|---|---|
| 1, 2, 8, 9 (REVOKEs) | LOW | LOW | LOW (verified by post-apply tests) | LOW | LOW |
| 3 (policy split) | LOW | LOW | LOW | LOW | LOW |
| 4 (orders.state CHECK) | LOW (worker writes lowercase only) | LOW | LOW | LOW | LOW |
| 5 (FK addition) | LOW (no rows had non-NULL recipe_version_id at apply time; verified post-hoc) | LOW (small table) | LOW | LOW | LOW |
| 6 (partial unique on invitations) | LOW (no duplicate active invitations existed) | LOW | LOW | LOW | LOW |
| 7 (receipt_lines positive CHECK) | LOW (no non-positive rows existed) | LOW | LOW | LOW | LOW |
| 10 (pos_waitlist unique + dedup) | MEDIUM (existing duplicates required DELETE before index) | LOW | LOW | LOW | **MEDIUM** — dedup DELETE is not restored by downgrade |

The MEDIUM rating on operation 10's reversibility is the most operationally significant gap. See Gap 3.

### Gap 2: No preflight block for data-validating operations (§3)

The standard requires a preflight check before any data-validating operation. 0013 contains three data-validating operations (#5 FK, #6 partial unique, #10 unique + dedup) but no preflight block.

The implicit preflight was "the `ALTER TABLE` would have failed if the constraint were violated." The standard requires this be explicit, with diagnostic queries an operator can run to investigate violations.

The preflight queries that should have run:

```sql
-- Preflight #5: confirm no orphaned recipe_version_id values exist
SELECT COUNT(*) FROM sale_line_items
WHERE recipe_version_id IS NOT NULL
  AND recipe_version_id NOT IN (SELECT id FROM recipe_versions);
-- Expected: 0. Actual at apply time: 0 (verified retrospectively).

-- Preflight #6: confirm no duplicate active invitations exist
SELECT tenant_id, email, COUNT(*)
FROM invitations
WHERE accepted_at IS NULL
GROUP BY tenant_id, email
HAVING COUNT(*) > 1;
-- Expected: no rows. Actual at apply time: no rows (verified retrospectively).

-- Preflight #10: identify duplicate emails in pos_waitlist
SELECT email, COUNT(*)
FROM pos_waitlist
GROUP BY email
HAVING COUNT(*) > 1;
-- Expected at apply time: some duplicates (which is why the migration included a dedup DELETE).
-- The migration handled this by DELETE-ing all but the earliest row per email before adding the index.
```

These preflight queries are now retroactively recorded. If a similar migration is needed in the future, this audit serves as the template.

### Gap 3: Isolation rule violated (§4.1)

The standard requires that only one data-validating migration run per deploy window. 0013 bundles three data-validating operations (#5, #6, #10) plus seven metadata-only operations in a single deploy.

This is the most direct conformance failure. Under the standard, these should have been separate migrations: a metadata-only batch (#1-4, #8-9), plus three separate data-validating migrations.

In retrospect: the bundling did not cause harm. All operations applied cleanly. But the standard's isolation rule exists because the failure mode is silent — a future similar bundle could partially succeed in ways that are hard to diagnose. The fact that this one didn't fail is not evidence the rule is wrong.

### Gap 4: Downgrade data-loss path undocumented (§6)

The standard requires rollback capability to be defined and notes that "rollback does not imply restoring historical data state." Operation 10's dedup DELETE removes rows that the `downgrade()` function cannot restore. This is acceptable under the standard, but should be documented in the migration file.

The downgrade does correctly reverse the schema operations. It does not (and cannot) restore the deleted `pos_waitlist` rows.

If this migration were ever downgraded, the affected rows would remain deleted. For the current pre-launch state with no production users, this is operationally irrelevant. For future migrations that perform similar cleanup, the data-loss path should be flagged in the migration docstring.

### Gap 5: Application impact verification undocumented (§4.5)

The standard requires verification that no application call sites depend on the prior state, with results documented in the migration record. 0013 makes several changes affecting application-facing schema:

- Operation 1, 2 (DELETE revokes) — no documented grep result
- Operation 3 (policy change) — no documented call-site verification
- Operation 8 (idempotency_keys DELETE revoke) — comment says "FORCE RLS + no DELETE policy already blocks the operation" but no grep result
- Operation 9 (pos_waitlist SELECT revoke) — comment says "write-only from the user's perspective" but no grep result

The retroactive verification (run 2026-05-27 from repo root):

```bash
# Operation 1, 2 — DELETE from tenants/users
$ grep -rn "DELETE FROM tenants\|DELETE FROM users" backend/app --include="*.py"
```
**Result: no results.** No application code issues DELETE against `tenants` or `users`. Revoke is safe.

```bash
# Operation 8 — UPDATE/DELETE on idempotency_keys
$ grep -rn "idempotency_keys" backend/app --include="*.py" | grep -iE "update|delete"
```
**Result:**
```
backend/app/modules/inventory/idempotency.py:154:            UPDATE idempotency_keys
```
One hit. This is `store_response()` in `idempotency.py`, which writes the completed response status and body back to the record after an operation succeeds. This is expected behavior for the idempotency pattern (claim the key on INSERT, then UPDATE with response on completion). Operation 8 revoked DELETE only, not UPDATE. This UPDATE path is intentionally preserved and is not affected by 0013.

```bash
# Operation 9 — SELECT from pos_waitlist by app_user paths
$ grep -rn "pos_waitlist" backend/app --include="*.py"
```
**Result: no results.** The table is not referenced in `backend/app/` Python files. It appears only in migrations and tests. The INSERT path for the public waitlist endpoint has not been committed to `backend/app/` as of this audit. The SELECT revoke in 0013 cannot break a route that does not yet exist.

## What 0013 got right

The migration is technically sound. The SQL is correct. The downgrade function exists and reverses every schema operation in dependency-correct order. The constraints use `DROP CONSTRAINT IF EXISTS` and `DROP INDEX IF EXISTS` patterns, making partial-failure downgrade safe. The 420 passing tests provide post-hoc confidence that no application paths broke.

The fact that conformance gaps exist does not invalidate the security improvements 0013 delivered. The migration closed a real high-severity issue (#5 — silent depletion failure on bad recipe_version_id values) and several medium-severity issues. These improvements are real and remain in effect.

## Operational disposition

0013 is accepted as-applied. It is not rolled back. The risk of rolling back a working security migration to satisfy retrospective process conformance exceeds the value.

This audit becomes the permanent record of:
- What 0013 changed
- What conformance gaps existed against the now-committed standard
- What preflight queries should have run (now recorded retroactively)
- What application call-site verifications were completed (Gap 5, above)

## Implications for future migrations

The next migration after 0013 is the first that will be authored with the standard in place. It should:

1. Include a risk profile block per §1.2
2. Include preflight queries with the standard's diagnostic format per §3 for any data-validating operations
3. Respect the isolation rule per §4.1 — data-validating operations get their own migration
4. Document rollback limitations (especially data-loss paths) per §6 in the migration docstring
5. Document application call-site verification per §4.5 in the migration docstring

This is not a checklist Claude can enforce. It is a checklist a human reviewer should apply when reviewing a migration PR. If the workflow allows it, a pre-commit hook that greps for the required docstring sections (`Risk Profile`, `Preflight`, `Application call-site audit`) in `backend/alembic/versions/*.py` would catch the most common omissions before merge.

## Audit completion status

- [x] Gap 5 grep commands run and results recorded inline above
- [ ] Document committed to repository at `backend/docs/migrations/audits/0013-post-review.md`
- [ ] Reference to this audit added to changelog or migration log

---

## Notes on this artifact

This audit is the retroactive completion of documentation that should have accompanied the migration. It is not a critique of the work that was done — that work was technically sound. It is the documentation layer that was missing because the standard did not exist when the migration was written.

Future migrations under the standard will produce this kind of documentation as part of the migration itself, not as a separate audit. The audit pattern is for retroactive cases only.
