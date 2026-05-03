# Release Risk Register

Risks that could delay or break the v1 pilot launch. Each row is owned, dated, and re-scored at sprint boundaries.

Severity: **P1** = launch blocker. **P2** = mitigation required pre-pilot. **P3** = monitor.

| # | Risk                                                                                          | Severity | Likelihood | Mitigation                                                                                                            | Triggers re-score              |
|---|-----------------------------------------------------------------------------------------------|----------|------------|-----------------------------------------------------------------------------------------------------------------------|--------------------------------|
| 1 | Clover webhook duplicates cause double inventory depletion                                    | P1       | Medium     | Durable `pos_event_inbox` with unique vendor event id; worker is idempotent; covered by Sprint 4 + 5 fixture tests.    | Sprint 4 / 5 exit              |
| 2 | RLS bypass leaks tenant data                                                                  | P1       | Low        | Sprint 2 hard exit gate runs cross-tenant access tests; CI enforces RLS-on for every new tenant table.                 | Every migration                |
| 3 | Anthropic extraction silently commits unverified data                                         | P1       | Low        | Receipt commit endpoint requires explicit `confirmed_at` + per-line user acknowledgement; no auto-commit code path.    | Sprint 6 exit                  |
| 4 | Postmark outage delays PO send and confuses owners                                            | P2       | Medium     | Outbox + DLQ; UI shows pending status; alert on DLQ growth.                                                            | Sprint 7 exit                  |
| 5 | Clerk outage blocks logins                                                                    | P2       | Low        | JWKS cached; existing sessions continue until JWT expiry; degraded-auth banner in app.                                 | Sprint 2 exit                  |
| 6 | Postgres restore drill never tested before first customer                                     | P1       | Low        | Sprint 11 builds drill; pre-pilot gate in Sprint 12.                                                                   | Sprint 11 exit                 |
| 7 | Forecast batch double-runs across DST or duplicate dispatcher tick                            | P2       | Medium     | `batch_runs` unique on `(tenant_id, batch_name, local_date)`; DST guard test in Sprint 8.                              | Sprint 8 exit                  |
| 8 | Receipt photos contain PII (handwritten notes) and leak via logs or extraction prompts        | P2       | Medium     | Photos stored in Spaces with signed URLs; raw photo content never logged; extraction prompts include only OCR text.    | Sprint 6 exit                  |
| 9 | App Store / Play Store rejection on first submission delays pilot                             | P2       | Medium     | TestFlight + internal track in Sprint 12; pre-fill metadata; legal EN/FR review pre-submission.                        | Sprint 12 exit                 |
|10 | Mobile API drift between FastAPI OpenAPI and generated TS client                              | P2       | High       | CI fails when generated types diverge from committed `api-client/`. Sprint 10 sets this up.                            | Sprint 10 exit                 |
|11 | Owner-only PO actions accidentally exposed to Manager via UI                                  | P1       | Low        | Server-side role guard authoritative. UI hiding is best-effort only; tests assert server rejection.                    | Sprint 7 exit                  |
|12 | Pilot count exceeds 10 quietly, breaking pricing promise                                      | P3       | Low        | Operational counter + alert at 8 active pilot tenants; manual gate before tenant 11.                                   | Sprint 12 exit                 |
|13 | Better Stack logging accidentally records JWTs / receipt extraction tokens                    | P2       | Medium     | Central redact filter on logger; secret-pattern test in CI.                                                            | Sprint 11 exit                 |
|14 | Inventory integrity check finds drift but no human sees it                                    | P2       | Medium     | Mismatch writes `admin_audit_log` row + outbox notification to founder; verify in Sprint 8.                            | Sprint 8 / 11 exit             |
|15 | EN/FR string drift — feature ships in English only                                            | P2       | High       | `decisions/bilingual-string-inventory.md` is the source of truth; CI lints `strings.ts` for missing FR keys.           | Every UI sprint                |
|16 | Solo founder unavailable when P1 fires                                                        | P3       | Medium     | P1 alerts via SMS + email + push; runbook documents 30-min response window during pilot hours.                         | Sprint 11 exit                 |
|17 | Manual receipt entry is so slow that staff stop using the app                                 | P2       | Medium     | Manual fallback timed during Sprint 6; target ≤ 90 sec for 5-line receipt.                                             | Sprint 6 / 12 exit             |

## Process

- Risks are reviewed at every sprint exit gate.
- New risks discovered during a sprint are appended with PR link.
- Severity downgrade requires evidence (test, drill, runbook).
- Severity upgrade is unilateral and triggers immediate planning re-shuffle.
