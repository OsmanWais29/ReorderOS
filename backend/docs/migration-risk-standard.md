# Migration Risk Standard

**Status:** Active
**Owner:** Platform / Schema Governance
**Last reviewed:** 2026-05-26

This document defines how database migrations are **classified, reviewed, and deployed safely**. It specifies **risk rules and required guarantees**, not implementation techniques.

If a migration cannot be expressed using these rules, either:

* the migration is unsafe, or
* this standard must be updated.

---

# 1. Risk classification model

Every migration must be classified across five independent risk dimensions.

---

## 1.1 Risk dimensions

| Dimension                 | Question                                                   | Low                       | Medium                               | High                                        |
| ------------------------- | ---------------------------------------------------------- | ------------------------- | ------------------------------------ | ------------------------------------------- |
| Data validity             | Could existing data violate the new rule?                  | No existing data affected | Some existing data may violate rules | Large-scale existing data may violate rules |
| Availability impact       | Does the change impact running system availability?        | No impact                 | Temporary performance or lock impact | Potential blocking or downtime risk         |
| Application compatibility | Can current application safely run after change?           | Fully compatible          | Requires coordinated deployment      | Breaks current application behavior         |
| Data propagation risk     | Does change affect replication or propagation consistency? | No impact                 | Delayed propagation risk             | Risk of inconsistency or backlog            |
| Reversibility             | Can the change be safely undone?                           | Fully reversible          | Requires manual steps                | Not safely reversible                       |

---

## 1.2 Required risk profile

Every migration must include a risk classification block:

* All five dimensions must be explicitly rated
* Each Medium or High rating must include justification
* No migration may omit this section

---

# 2. Migration classification

Migrations are classified into two categories.

---

## 2.1 Metadata migrations

A migration is metadata if it only changes schema structure or permissions without requiring validation of existing data.

Includes:

* new tables
* nullable columns
* indexes
* views
* permissions and access rules

### Properties:

* safe to batch
* no data validation required
* low operational risk

---

## 2.2 Data-validating migrations

A migration is data-validating if it enforces rules over existing data.

Includes:

* constraints on existing rows
* uniqueness rules on populated tables
* referential integrity enforcement
* tightening schema invariants

### Properties:

* must run alone per deploy window
* requires pre-deployment validation
* must account for existing data state

---

# 3. Pre-deployment validation requirement

All data-validating migrations must include a pre-deployment validation step.

---

## 3.1 Purpose

Validation ensures:

* rule will not immediately fail in production
* violations are detected before enforcement
* failure is explicit and actionable

---

## 3.2 Requirements

Validation must:

* detect rule violations
* count affected rows
* stop execution if violations exist
* provide diagnostic information

---

## 3.3 Failure behavior

If validation fails:

* migration must stop immediately
* no schema change is applied
* failure must be explicit and actionable

---

# 4. Migration execution rules

## 4.1 Isolation rule

Only one data-validating migration may run per deployment window.

---

## 4.2 Ordering rule

Migrations must execute in a strict linear sequence.

No branching or parallel execution paths.

---

## 4.3 Safe deployment principle

Migrations must ensure:

* no corruption of running system state
* partial failure leaves system recoverable
* final state is consistent

---

## 4.4 Compatibility rule

If application changes are required:

* compatibility must be ensured before enforcement
* or schema must remain backward compatible until deployed application supports it

---

## 4.5 Application impact verification rule

If a migration affects application-facing schema (any of the following):

* column removal or rename
* constraint tightening on used fields
* permission changes affecting access paths
* structural changes to query-relevant tables

Then the migration must:

* explicitly verify all application call sites impacted
* document verification results in the migration record
* ensure changes are deployed in a compatible order with application updates

This verification is required even if the migration is otherwise classified as metadata-only.

---

# 5. Constraint introduction rules

When introducing constraints on existing data:

* constraints must not immediately break production data
* existing data must be validated before enforcement
* enforcement must be the final step, not the first

---

# 6. Reversibility requirement

All migrations must define rollback capability:

* metadata migrations: reversible without data loss
* data-validating migrations: reversible at schema level where possible

Rollback does not imply restoring historical data state.

---

# 7. Production safety principle

Assume:

> The database is always live and cannot be taken offline.

Therefore:

* no downtime assumptions
* no unbounded locking operations without justification
* no exclusive database control assumptions

---

# 8. Failure handling principle

Migrations must assume:

* failures will occur
* operators may not be authors
* failures must be diagnosable without external context

Therefore:

* failures must be explicit
* validation failures must be self-explanatory
* error outputs must guide resolution

---

# 9. Governance rule

Any migration that cannot conform:

* is rejected, or
* requires updating this standard first
