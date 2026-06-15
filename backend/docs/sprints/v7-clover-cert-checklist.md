# V7 — Clover sandbox certification checklist & runbook

The agent-prepared scaffolding so the founder's "Clover sandbox afternoon" is *running a checklist*,
not figuring out what to check. Split into: (A) what's already automated/verified offline, (B) the
live sandbox runbook (founder, real credentials), (C) parser-conformance items that need a real
payload to confirm (flagged honestly — the docs were ambiguous).

Sources consulted (Clover dev docs, 2026-06): working-with-orders, creating-custom-orders,
expanding-fields, working-with-transaction-data-rest. The REST reference did not expose a complete
field-by-field Order JSON schema, so items in (C) are marked verify-in-sandbox rather than asserted.

---

## A. Automated / verified offline (no Clover account needed)

- [x] **Payment-state derivation matrix** — `tests/test_v7_clover_conformance.py` exercises every
  branch of `_derive_payment_state`: explicit `paymentState`, `refundTotal` vs `total`,
  `payments.elements[].result` (SUCCESS/AUTH + REFUNDED/REFUND mixes), `payType`, default OPEN, and
  the priority ordering between them.
- [x] **Modifier multiplier double-representation** — Clover may send "Extra shot ×2" as one
  modification with `quantity: 2` OR two modifications each defaulting to 1; the worker sums to 2
  either way. Both representations tested → same depletion.
- [x] **Line/order field extraction** — a representative real-shape order drives the real worker
  (reusing the V5b sim harness) and the resulting `orders`/`sale_line_items` + depletion are checked.
- [x] **Non-locked orders skipped**, **refunded/voided line flags** (`refunded`→is_refunded,
  `exchanged`→is_voided), **unmapped item** (`item.id` with no matching `menu_items.pos_item_id`) —
  covered by V2/V3/V5 + conformance tests.
- [x] **Idempotent re-delivery** — same order via two webhooks doesn't double
  (`test_v5b_duplicate_delivery_idempotent`; inbox dedup + `uq_sli_clover`).
- [x] **Webhook signature verification** mandatory (security playbook; existing phase-6 tests).
- [x] **End-to-end depletion correctness under load** — V5b 2,000-order/3-terminal sim vs oracle.

## B. Live sandbox runbook (FOUNDER — real credentials, test merchant)

Prereqs: a Clover **sandbox** developer account, a test merchant, the app's sandbox App ID/secret in
`CLOVER_APP_ID`/`CLOVER_APP_SECRET`, `CLOVER_ENVIRONMENT=sandbox`, a tunnel for webhook delivery.

1. [ ] **OAuth install flow** — install the app on the test merchant; confirm the callback stores an
   encrypted token + `tenant_pos_connections.state='active'` with the real `connection_id`.
2. [ ] **Catalog sync** — run `POST /pos/clover/sync-menu`; confirm `menu_items.pos_item_id` and
   `modifiers.pos_modifier_id` populate from the real catalog (these are the keys the worker maps by).
3. [ ] **Webhook subscription** — confirm order webhooks arrive, signature verifies, and a row lands
   in `pos_event_inbox` (state pending).
4. [ ] **Ring up a real sandbox sale** (locked + paid) on the test merchant → worker ingests →
   `sale_line_items` created → depletion movements written. Compare the real ledger to a hand-计算
   expectation for that order (the V5 oracle math by hand for one order).
5. [ ] **Refund a sandbox sale** → confirm `is_refunded` flips and `reverse_line` nets the movements.
6. [ ] **Token refresh** — force/await token expiry; confirm refresh path works (no manual re-auth).
7. [ ] **Uninstall** — uninstall the app; confirm graceful handling (connection state, no crashes).
8. [ ] **Capture 3-5 real order payloads** (paid, refunded, with-modifiers, multi-line) and drop them
   into `tests/fixtures/clover/` → flip the (C) items below from verify-in-sandbox to asserted.

## C. Parser-conformance items to CONFIRM against a real payload (honest unknowns)

The worker assumes these shapes; the docs were ambiguous, so confirm each against a captured sandbox
order and adjust the parser if wrong:

- [ ] **`modifications.elements[].quantity`** — does Clover include a per-modification `quantity`, or
  only repeated elements? (Worker handles both, but confirm the real shape so the assumption is known.)
- [ ] **`refundTotal`** — is it populated on the order by default, or only via `?expand=refunds`?
  `_derive_payment_state` reads `order_data.get("refundTotal")`; if it needs expansion, the fetch in
  `process_event` (`clover.get_order`) must request it, else the refund-total branch is dead and we
  fall back to `payments`/`paymentState`.
- [ ] **`payments.elements[].result`** — confirm the real values (we match SUCCESS/AUTH/REFUNDED/REFUND).
- [ ] **`paymentState`** — confirm Clover emits PAID / CREDITED / REFUNDED / PARTIALLY_REFUNDED with
  the spellings we map (`_PAYMENT_STATE_PRIORITY`).
- [ ] **`item.id` on line items** — confirm the line→catalog reference is `lineItem.item.id` (vs an
  embedded item object) so the `menu_items.pos_item_id` join holds.
- [ ] **`unitQty` semantics** — confirm it is the count (1 = one unit), and how fractional/weighted
  items are represented (our model treats qty as a plain multiplier).

## Cert gate

V7 is "certified" when: (A) green in CI, (B) all live steps checked off on a real sandbox merchant,
(C) every conformance unknown confirmed (or the parser fixed + a fixture added). Until (B)/(C), V7 is
**scaffolding-complete, live-cert-pending** — do not tell a café "Clover-certified" yet.
