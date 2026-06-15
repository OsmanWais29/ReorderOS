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

## D. THE single-sale end-to-end trace (the one thing still untested)

Every automated test injects into `pos_event_inbox` (or sets `fetched_payload`), so the ARRIVAL
half is unproven: real Clover delivery, our `X-Clover-Auth` verification, and the worker fetching +
parsing the REAL order JSON. This is the walkthrough that proves it — ring up ONE sale, watch it
flow, check the DB at every hop. `psql` shorthand (point at the DB your app/worker actually use):
`PGPASSWORD=… psql -h <host> -p <port> -U <user> -d <db>` and `:T` = your tenant_id.

### Prereqs (so the sale actually depletes — do these once)
1. App configured for sandbox: `CLOVER_ENVIRONMENT=sandbox`, `CLOVER_APP_ID/_SECRET`,
   `CLOVER_OAUTH_CALLBACK_URL` (public), `CLOVER_WEBHOOK_AUTH_CODE`, `TOKEN_ENCRYPTION_KEY`,
   `DATABASE_URL`, `SERVICE_DATABASE_URL`. Webhook URL registered in the Clover dashboard =
   `https://<your-host>/api/v1/webhooks/pos/clover` (it must echo the `verificationCode` ping — that
   confirms reachability before anything else).
2. **Connect:** as an owner, hit `GET /api/v1/pos/clover/connect` → install on the sandbox merchant.
   Check: `SELECT merchant_id, state FROM tenant_pos_connections WHERE tenant_id=:T;` → one row,
   `state='active'`. (Without this the worker can't fetch the order — step 3 would stall.)
3. **Catalog + recipe:** `POST /api/v1/pos/clover/sync-menu`, then confirm a recipe for the item
   you'll ring up. Check: `SELECT name, pos_item_id, recipe_version_id FROM menu_items WHERE
   tenant_id=:T AND recipe_version_id IS NOT NULL;` → the item has a non-null `recipe_version_id`.
   (If it doesn't, the sale will land as `unmapped/no_recipe` — correct behavior, but not the
   depletion proof you want.)
4. **Start the worker:** `python -m app.workers.inbox_worker` (leave it running; watch its logs).

### Ring up ONE sale and trace it
5. In the Clover **sandbox Register**, ring up that mapped item (qty 1), take payment with a test
   card, and **close/lock the order** (depletion needs `state=locked` + `paymentState=PAID`).

6. **Hop 1 — Clover has it:** the order shows in the Clover sandbox dashboard (Orders). (Not our DB.)

7. **Hop 2 — webhook → inbox (arrival proven):**
   ```sql
   SELECT inbox_id, vendor_object_type, vendor_event_type, state,
          signature_verified, (fetched_payload IS NULL) AS not_yet_fetched, received_at
   FROM pos_event_inbox WHERE tenant_id=:T ORDER BY received_at DESC LIMIT 3;
   ```
   Expect a fresh row: `vendor_object_type='O'`, `signature_verified=true`, `state='pending'`,
   `not_yet_fetched=true`. **No row ⇒ delivery or auth failed** — check the Clover webhook config and
   that `X-Clover-Auth` matches `CLOVER_WEBHOOK_AUTH_CODE` (the handler 401s on mismatch); check the
   merchant resolves (`SELECT lookup_tenant_by_merchant('clover','<merchant_id>');`).

8. **Hop 3 — worker fetched + parsed the REAL order (the §C confirmation moment):**
   ```sql
   SELECT state, (fetched_payload IS NOT NULL) AS fetched, processing_error
   FROM pos_event_inbox WHERE inbox_id='<from hop 2>';
   -- then inspect the REAL Clover JSON we received:
   SELECT jsonb_pretty(fetched_payload) FROM pos_event_inbox WHERE inbox_id='<…>';
   ```
   Expect `state='processed'`, `fetched=true`. **Read `fetched_payload`** — this is the real Clover
   order shape; confirm §C against it: `lineItems.elements[].item.id`, `unitQty`, `paymentState`,
   `refundTotal` present-or-not, `payments.elements[].result`, and (if you added a modifier)
   `modifications.elements[].quantity` vs repeated elements. **Stuck `pending`/`processing` ⇒ worker
   down or `get_order` failing** (check worker logs + connection `state='active'`).

9. **Hop 4 — order + line parsed correctly:**
   ```sql
   SELECT id, clover_order_id, state, payment_state FROM orders
   WHERE tenant_id=:T ORDER BY processed_at DESC LIMIT 1;            -- expect locked / PAID
   SELECT clover_line_item_id, menu_item_id, recipe_version_id, quantity,
          depletion_status, depletion_reason
   FROM sale_line_items WHERE order_id='<order id>';
   ```
   Expect one line, `menu_item_id` + `recipe_version_id` non-null, `depletion_status='depleted'`.
   `unmapped` ⇒ item not mapped (prereq 3); `failed` ⇒ read `depletion_reason`
   (`missing_conversion` / `sale_ineligible` / etc.).

10. **Hop 5 — the ledger moved (the payoff):**
    ```sql
    SELECT inventory_item_id, movement_type, delta, idempotency_key
    FROM inventory_movements WHERE source_id='<sale_line_item id>' ORDER BY inventory_item_id;
    ```
    Expect `sale_depletion` rows (Mode A, negative δ) and/or `sale_signal` (Mode B, positive) — one
    per ingredient — with δ = `qty × recipe_qty / yield` (hand-check against the recipe). Confirm the
    derived stock: call `on_hand()` for an ingredient (or read `vw_depletion_coverage`).

11. **Idempotency sanity:** in Clover, re-save the same order (fires another webhook). Re-run hop 10
    — δ must be UNCHANGED (no doubling: inbox dedup + `uq_sli_clover`). Then refund the sale in Clover
    and confirm reversal rows appear and net the ingredient to zero.

If all five hops check out for one sale, the full ingestion path is proven end-to-end against real
Clover — the half the 2,000-order sim could not reach.

## Cert gate

V7 is "certified" when: (A) green in CI, (B) all live steps checked off on a real sandbox merchant,
(C) every conformance unknown confirmed (or the parser fixed + a fixture added). Until (B)/(C), V7 is
**scaffolding-complete, live-cert-pending** — do not tell a café "Clover-certified" yet.
