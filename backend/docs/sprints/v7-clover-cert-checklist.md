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

## E. PRE-FLIGHT (run all four BEFORE ringing up — a missing prereq must not masquerade as broken ingestion)

All four must be green before step 5. Each is a positive check, not an assumption.

1. **Webhook reachable + verification echo works** (the reachability proof). Active self-test of the
   exact verify path — no dashboard needed:
   ```
   curl -sS -X POST https://<your-host>/api/v1/webhooks/pos/clover \
        -H 'Content-Type: application/json' -d '{"verificationCode":"preflight-123"}'
   ```
   GREEN = it prints `preflight-123` (200, text/plain). RED = timeout/404/wrong body → the URL Clover
   posts to is wrong/unreachable; fix before anything. Then confirm the Clover dashboard shows the
   webhook **verified/active** (Clover sent its own ping and got the echo).

2. **Connection active + token valid:**
   ```sql
   SELECT merchant_id, state, (access_token_expires_at > now()) AS token_valid
   FROM tenant_pos_connections WHERE tenant_id=:T AND vendor='clover';
   ```
   GREEN = one row, `state='active'`, `token_valid=t`. RED = no row / `state<>'active'` / token expired
   → run the **connect-initiation flow** below (NOT a raw browser hit to `/connect`).

   ### Connect-initiation flow (the exact way to start the OAuth handshake)
   `GET /api/v1/pos/clover/connect` is owner-auth via **Bearer JWT** — a plain browser navigation
   (address bar / link click) **cannot attach the Authorization header**, so it 401s and never
   redirects to Clover → you "land on /callback with no code." The real flow:
   1. Authenticated owner calls the JSON endpoint **with** the header:
      ```
      curl -sS https://<host>/api/v1/pos/clover/connect-url -H "Authorization: Bearer <owner-jwt>"
      # → {"url":"https://sandbox.dev.clover.com/oauth/v2/authorize?client_id=…&redirect_uri=…&state=…"}
      ```
   2. **Open that returned `url` in a browser** → Clover consent screen → approve.
   3. Clover redirects to `CLOVER_OAUTH_CALLBACK_URL?merchant_id=…&code=…&state=…`; `/callback`
      exchanges the code and writes the connection (then re-check the SQL above → `active`).

   **Authorize path is v2** (`/oauth/v2/authorize`) — fixed `c1b8720`; the legacy `/oauth/authorize`
   returns to the callback without a v2-usable code. Two snag-checks if you still get no code:
   - **`redirect_uri` must EXACTLY match** the redirect URL registered in the Clover sandbox app
     dashboard (character-for-character) — a mismatch makes Clover skip issuing the code.
   - **App-launch ≠ authorize:** opening the installed app *tile* from the Clover dashboard redirects
     to the site URL with `merchant_id` but **no `code`** (that's a launch). Only going through the
     `/oauth/v2/authorize` consent URL (step 2 above) yields a `code`.
   - **If you DO get a code but token exchange fails:** the callback posts to
     `apisandbox.dev.clover.com/oauth/v2/token`; the docs example uses `sandbox.dev.clover.com/oauth/v2/token`.
     If it 404s, the token host needs switching to `clover_oauth_base_url` (flagged, not yet changed).

3. **Test item mapped to a confirmed recipe that can actually deplete:**
   ```sql
   SELECT mi.name, mi.pos_item_id, mi.recipe_version_id, count(ri.*) AS ingredients
   FROM menu_items mi
   LEFT JOIN recipe_ingredients ri ON ri.recipe_version_id = mi.recipe_version_id
   WHERE mi.tenant_id=:T AND mi.recipe_version_id IS NOT NULL
   GROUP BY 1,2,3;
   ```
   GREEN = the item you'll ring up appears with a non-null `pos_item_id` AND `recipe_version_id` AND
   `ingredients >= 1`. RED = missing pos_item_id (re-sync catalog) / no recipe_version_id (confirm the
   recipe) / 0 ingredients (add ingredients + confirm). NOTE: if a recipe unit ≠ the item's storage
   unit, also confirm a conversion exists, or hop 4 will read `failed/missing_conversion`.

4. **Worker running:** confirm the process is up and looping —
   `ps aux | grep "[a]pp.workers.inbox_worker"` (or the platform's Worker component shows running) AND
   the log shows `inbox_worker.starting`. Liveness cross-check: there are no stale pending events —
   `SELECT count(*) FROM pos_event_inbox WHERE tenant_id=:T AND state='pending';` should be 0 now (and
   later, any new pending row drains within ~2s). RED = no process → start `python -m
   app.workers.inbox_worker`.

Only when 1-4 are green, proceed to step 5. (A prereq miss now produces a *clean diagnosis* later —
e.g. `unmapped` = prereq 3, stuck `pending` = prereq 4 — instead of looking like a broken pipeline.)

## F. What a §C shape mismatch looks like at Hop 3 — and stop-vs-note

A §C mismatch = the real Clover JSON in `fetched_payload` differs from where the parser reads. This is
a FINDING (the verification sprint doing its job — real-vs-assumed surfaced), not a failure. The fix is
usually one line (point the parser at the right field) + a captured-payload regression test that closes
the §C item. Decide stop-vs-note by ONE question: **does the mismatch corrupt depletion** (the
menu-item/recipe resolution, the quantity, the payment-state/eligibility, or the modifier mapping)?

**STOP-and-fix (silent depletion corruption — the dangerous ones):**
| Symptom in the data | Real cause | Fix |
|---|---|---|
| `sale_line_items.menu_item_id` NULL despite prereq 3 green → `depletion_status='unmapped/no_recipe'` | line→catalog ref isn't `lineItem.item.id` where `_insert_line_item` reads it | repoint to the real ref |
| `sale_line_items.quantity` wrong (e.g. 1000 not 1) → δ off at hop 5 | `unitQty` semantics differ (weighted/thousandths) | adjust qty parse |
| order ingested but `depletion_status='failed'/sale_ineligible` though you paid+locked | `_derive_payment_state` read the wrong field → OPEN | fix the paymentState/payments/refundTotal read |
| a refund doesn't reverse | `refundTotal` only via `?expand=refunds`, or different field | request the expand in `get_order` / fix detection |
| modified item: no `sale_line_item_modifiers` row → modifier δ missing at hop 5 | `modifications…modifier.id`/quantity shape differs | repoint modifier parse |

**NOTE-and-continue (no depletion impact):**
- Extra/unknown fields Clover sends that we don't read — ignored by design.
- A non-load-bearing field absent/renamed (`device.id`, `externalReferenceId`, `employee.id`) → a NULL
  in a reporting column; order still depletes correctly.
- Cosmetic differences (timestamp precision, field order).

Rule of thumb: **if hop 4 shows `depleted` and hop 5's δ matches your hand-check, any payload
difference is NOTE-and-continue.** If the sale lands `unmapped`/`failed`/wrong-δ when it should be a
clean `depleted`, that's STOP-and-fix — and exactly the bug this whole exercise exists to catch before
a real café relies on it. Either way: save the `fetched_payload` to `tests/fixtures/clover/` so the
fix (or the confirmation) becomes a permanent regression test and §C closes.

## Cert gate

V7 is "certified" when: (A) green in CI, (B) all live steps checked off on a real sandbox merchant,
(C) every conformance unknown confirmed (or the parser fixed + a fixture added). Until (B)/(C), V7 is
**scaffolding-complete, live-cert-pending** — do not tell a café "Clover-certified" yet.
