# Contradiction Register

Tracks every place v1 documents disagree with each other or with earlier architecture docs. Each contradiction must be **resolved** before the affected sprint starts.

| # | Topic                          | Source A                                          | Source B                                          | Resolution                                                                                                  | Status   |
|---|--------------------------------|---------------------------------------------------|---------------------------------------------------|-------------------------------------------------------------------------------------------------------------|----------|
| 1 | RBAC role set                  | Older arch docs include `ops_lead`, `line_cook`, `accountant` | v1 build plan: Owner / Manager / Staff only | v1 ships with three roles only. Extra roles deferred. Schema reserves an enum but UI won't expose them.       | Resolved |
| 2 | Pilot pricing currency         | SPRINTS.md says "$99 CAD/month"                   | v1-backend-build-plan.md says "USD 99/month"     | Use **USD 99/month/location**. Update SPRINTS.md when next touched.                                          | Resolved |
| 3 | PO send channel                | Some early docs imply SMS option                  | v1 plan: email-only, SMS deferred                 | Email-only via Postmark. SMS task `sms.dispatch` explicitly deferred.                                       | Resolved |
| 4 | Database vendor                | Older notes referenced Supabase                   | v1 plan: DigitalOcean Managed PostgreSQL          | DigitalOcean Managed PG. No Supabase dependencies anywhere in code.                                         | Resolved |
| 5 | Cross-tenant price intelligence| Old "Price and Integration Layer" doc designs cross-tenant aggregates | v1 lock: deferred until 50–100 tenants + legal | All cross-tenant reads disabled. `price.market_aggregate` task deferred. `ingredient_prices` is tenant-scoped. | Resolved |
| 6 | Receipt commit autonomy        | Anthropic extraction implies automation           | v1 lock: human review before commit               | Extraction always produces a draft. Commit endpoint requires user-confirmed payload + idempotency key.       | Resolved |
| 7 | Realtime / websockets          | Earlier dashboard arch hinted at live updates     | v1 lock: foreground polling only                  | No websocket layer in v1. Push notifications used for events only, not data sync.                            | Resolved |
| 8 | Suppliers data model           | Public-only vs tenant-only debated in arch docs   | v1 lock: hybrid `suppliers_master` + `tenant_suppliers` | Hybrid model. `suppliers_master` is read-only public; `tenant_suppliers` is RLS-protected.                  | Resolved |
| 9 | Localization scope             | Some docs assume server-side localized errors     | v1 lock: stable error codes; UI translates        | Server returns codes (e.g. `RECEIPT_DRAFT_REQUIRED`). Only owner-facing notifications are localized server-side.| Resolved |
|10 | Tenant time semantics          | "Nightly batch" vague                             | v1 design: tenant-local 02:00 + DST guard         | Each tenant stores `iana_timezone`. Nightly dispatcher schedules per-tenant. `batch_runs` keyed on tenant local date. | Resolved |
|11 | Inventory mutability           | Some early code assumed direct quantity edits     | v1 lock: append-only ledger is the only writer    | All quantity changes go through `inventory_movements`. `current_quantity` is materialized state, not authoritative. | Resolved |
|12 | Auth provider                  | Mixed mentions of Auth0 vs Clerk                  | v1 lock: Clerk                                    | Clerk only. JWKS cached; documented Clerk-outage degradation behavior in Sprint 2 exit gate.                  | Resolved |

## Open Items (must close before the indicated sprint)

| # | Topic | Decision needed | Blocks sprint |
|---|-------|-----------------|---------------|
| — | none open at this time |  |  |

> If any new contradiction surfaces during a sprint, append it here with status `Open` and link the PR. A sprint cannot exit with `Open` items in its dependency chain.
