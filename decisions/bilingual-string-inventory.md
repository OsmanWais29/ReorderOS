# Bilingual String Inventory (EN / FR)

Source of truth: `frontend/src/i18n/strings.ts`. This document tracks **coverage** — every customer-facing string surface must exist in both EN and FR before merge.

CI rule (Sprint 10): `strings.ts` is parsed; any key present in `en` but missing in `fr` (or vice-versa) fails the typecheck job.

## Surfaces

| Surface                       | Owner module (frontend)                    | EN coverage | FR coverage | Notes                                                    |
|-------------------------------|--------------------------------------------|:-----------:|:-----------:|----------------------------------------------------------|
| Welcome / marketing           | `app/onboarding/welcome.tsx`               | ✅          | ✅          | Headline 1+2, value props, CTAs.                          |
| Account creation              | `app/onboarding/account.tsx`               | ✅          | ✅          | Field labels, validation hints.                           |
| Push opt-in                   | `app/onboarding/push.tsx`                  | ✅          | ✅          |                                                          |
| POS picker                    | `app/onboarding/pos-picker.tsx`            | ✅          | ✅          | Provider names stay in English (brand).                   |
| Connecting / OAuth handoff    | `app/onboarding/connecting.tsx`            | ✅          | ✅          | Includes permission preview text.                         |
| Found summary                 | `app/onboarding/found-summary.tsx`         | ✅          | ✅          |                                                          |
| Cleanup (categorize menu)     | `app/onboarding/cleanup.tsx`               | ✅          | ✅          | Includes `{have}/{target}` interpolation.                 |
| Suppliers setup               | `app/onboarding/suppliers.tsx`             | ⚠️ pending  | ⚠️ pending  | Needs strings before Sprint 10.                           |
| Par levels                    | `app/onboarding/par-levels.tsx`            | ⚠️ pending  | ⚠️ pending  |                                                          |
| Team invite                   | `app/onboarding/team.tsx`                  | ⚠️ pending  | ⚠️ pending  |                                                          |
| PIN / biometric               | `app/onboarding/pin.tsx`, `biometric.tsx`  | ⚠️ pending  | ⚠️ pending  |                                                          |
| Billing (hidden in pilot)     | `app/onboarding/billing.tsx`               | ⚠️ deferred | ⚠️ deferred | Stripe surface; covered when billing un-hides post-pilot. |
| Done / handoff                | `app/onboarding/done.tsx`                  | ⚠️ pending  | ⚠️ pending  |                                                          |
| Tab: Home                     | `app/(app)/home.tsx`                       | ⚠️ pending  | ⚠️ pending  | Currently English-only literals; must move into `strings.ts`. |
| Tab: Stock                    | `app/(app)/stock.tsx`                      | ⚠️ pending  | ⚠️ pending  |                                                          |
| Tab: Orders                   | `app/(app)/orders.tsx`                     | ⚠️ pending  | ⚠️ pending  |                                                          |
| Tab: Sales                    | `app/(app)/sales.tsx`                      | ⚠️ pending  | ⚠️ pending  |                                                          |
| Tab: More                     | `app/(app)/more.tsx`                       | ⚠️ pending  | ⚠️ pending  | Lang toggle works; row labels still EN-only.              |
| Receipts (photo + review)     | (not built yet)                            | ⛔ todo     | ⛔ todo     | Sprint 6 deliverable.                                     |
| Purchase orders               | (not built yet)                            | ⛔ todo     | ⛔ todo     | Sprint 7 deliverable.                                     |
| Errors / validation toasts    | (cross-cutting)                            | ⛔ todo     | ⛔ todo     | Map server error codes → translated copy.                 |
| Push notification bodies      | server-side templates                      | ⛔ todo     | ⛔ todo     | Sprint 7 outbox / Sprint 11 ops alerts.                   |
| Email templates (PO send)     | server-side templates                      | ⛔ todo     | ⛔ todo     | Sprint 7. Templates per tenant-language preference.       |
| App Store / Play descriptions | store metadata                             | ⛔ todo     | ⛔ todo     | Sprint 12 legal review.                                   |

Legend: ✅ in `strings.ts` · ⚠️ partial (literals exist but not localized) · ⛔ not started · deferred = product-locked out of v1 pilot.

## Required Keys Not Yet In `strings.ts`

These must land before Sprint 10 exit (mobile rebuild):

```
home.greeting.morning, home.greeting.afternoon, home.greeting.evening
home.section.thisWeek, home.stat.sales, home.stat.foodCost, home.stat.openPos
stock.tab.title, stock.empty, stock.par.below, stock.par.atRisk, stock.count.cta
orders.tab.title, orders.empty, orders.status.draft, orders.status.approved,
orders.status.dispatched, orders.status.received, orders.status.partial,
orders.status.canceled, orders.status.failed
sales.tab.title, sales.range.day, sales.range.week, sales.range.month
more.title, more.row.team, more.row.suppliers, more.row.billing, more.row.signOut
errors.network, errors.session.expired, errors.permission.denied,
errors.tenant.missing, errors.idempotency.replayed,
errors.receipt.draftRequired, errors.receipt.confirmRequired,
errors.po.ownerOnly, errors.po.invalidTransition
notifications.po.dispatched, notifications.po.deliveryFailed,
notifications.stock.belowPar, notifications.integrity.mismatch
```

## Bilingual Process

1. New UI surface lands → English keys merged first.
2. Same PR adds the FR key with the literal "[FR-TODO]" placeholder if translation is pending.
3. Pre-merge CI fails if any key has the placeholder marker (Sprint 10 work).
4. FR review batched weekly with a Quebec French reviewer.
5. Server returns stable error codes; UI maps them to translated copy under `errors.*`.

## Out Of Scope For v1

- Right-to-left languages.
- Locale-aware number / currency formatting (we use `Intl` defaults; revisit when EU/UK pilots happen).
- Spanish, German, etc.
