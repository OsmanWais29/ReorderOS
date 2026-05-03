# Trigger.dev Task Backlog

These are the background jobs and operational tasks that should exist in Trigger.dev or an equivalent worker/scheduler.

## V1 Required Tasks

| Task id | Purpose | Trigger | Hard rule |
| --- | --- | --- | --- |
| `clover.inbox.process` | Process pending Clover webhook inbox rows | continuous/polled | idempotent, fast retry |
| `clover.reconcile` | Pull missed Clover sales | scheduled | dedupe by vendor event id |
| `inventory.integrity_check` | Compare ledger sum to current quantity | nightly | P1 on mismatch |
| `forecast.nightly` | Run deterministic forecast and draft POs | tenant-local 2am | no LLM |
| `receipt.extract` | Run Anthropic receipt extraction | on upload | draft only, no auto-commit |
| `outbox.dispatch` | Send email/push side effects | continuous/polled | after DB commit only |
| `exports.generate` | Generate compliance/bookkeeper exports | on demand | signed link expires |
| `dr.restore_smoke_test` | Validate backup restore path | pre-launch/manual, then quarterly | must pass before first customer |

## Deferred Tasks

| Task id | Reason deferred |
| --- | --- |
| `sms.dispatch` | SMS deferred after v1 |
| `price.market_aggregate` | cross-tenant price comparison deferred until 50-100 restaurants plus legal review |
| `accounting.push` | direct accounting push deferred |

## Task Design Rules

- Task payloads contain IDs, never large raw blobs.
- Task handlers re-read current database state.
- Every external call has timeout, retry, and structured logging.
- Every task is safe to retry.
- Every task writes success/failure audit or operational status.
- Dead-lettered tasks create admin alerts.

