# V7 — Clover sandbox afternoon: the complete beginner walkthrough

A from-scratch, plain-language guide to proving ONE real Clover sale flows all the way into the
inventory ledger. No prior Clover/OAuth experience assumed. Companion to
`v7-clover-cert-checklist.md` (which is the terse reference); this is the hand-held version.

---

## 0. What you're doing and why (read this first)

Our automated tests prove everything that happens *after* a sale lands in our database. They do NOT
prove a real sale gets *from Clover into our database* — that path (Clover → webhook → our worker →
ledger) has never run against the real Clover. This afternoon proves exactly that, with one sale,
checking the database at each step so you can see it actually arrived and depleted correctly.

**The journey one sale takes:**
```
You ring up a sale on the Clover test register
        │
        ▼  Clover sends a "something happened to order X" webhook
Our webhook endpoint  → writes a row to the `pos_event_inbox` table
        │
        ▼  our background "worker" picks it up
Worker fetches the FULL order from Clover, parses it
        │
        ▼  writes the order + line items, then computes depletion
`inventory_movements` rows appear  → stock goes down
```
You'll watch each arrow happen.

## 1. Words you'll see (plain English)

- **Sandbox** — Clover's free pretend environment. Fake merchant, fake card, no real money.
- **Test merchant** — a pretend restaurant inside the sandbox that "uses" our app.
- **OAuth / "connect"** — the secure handshake where the merchant grants our app permission to read
  their orders. It ends with our app holding an access token for that merchant.
- **Webhook** — Clover phoning our server to say "order X changed." It does NOT send the full order,
  just a notification; our worker fetches the details.
- **Worker** — a background program (`inbox_worker`) that processes those notifications.
- **Inbox** — the `pos_event_inbox` table where notifications wait to be processed.
- **Ledger** — the `inventory_movements` table; each sale writes negative rows (stock consumed).
- **JWT / Bearer token** — your login token. You attach it to API calls as proof of who you are.

## 2. What you need before you start

Accounts & access:
- A **Clover sandbox developer account** (free) and the ability to create a test merchant.
- The **deployed app URL** (e.g. `https://reorderos-api-7d4et.ondigitalocean.app`).
- **Database access** to the deployed Postgres (DigitalOcean → your database → "Connection details":
  host, port, user, password, db name, and the CA cert or `sslmode=require`).
- An **owner login** (email + password) for a tenant in the app. If you don't have one, register one
  first via the app's onboarding (creates a WorkOS user + a tenant with you as owner).

Tools on your computer (all free):
- A **terminal** (macOS Terminal / Windows PowerShell or WSL).
- **curl** (preinstalled on mac/Linux; on Windows use PowerShell's `curl.exe`).
- **python3** (for reading JSON in commands). Check: `python3 --version`.
- **psql** (Postgres client) to run the database checks. Check: `psql --version`. (Install via
  `brew install libpq` on mac, or use any DB GUI like TablePlus/DBeaver instead.)

> Tip: keep a notes file open. You'll collect a few values: API URL, owner email/password, your
> `tenant_id`, the Clover merchant_id, and the DB connection string.

---

## PART 1 — Set up Clover (one time, in the Clover Developer Dashboard)

1. **Create a sandbox developer account** at the Clover developer site and sign in to the Developer
   Dashboard.
2. **Create a test merchant**: Developer Dashboard → *Test Merchants* → create one (any
   region/currency is fine). This is your pretend restaurant.
3. **Create your app** (if not already): *Your Apps* → create app → choose **Web app** (this exposes
   the REST Configuration we need).
4. **Set REST permissions**: in *App Settings → REST Configuration*, grant at least **Read** for
   Orders, Inventory, Merchant, Payments, Employees (our app requests
   `ORDERS_R, EMPLOYEES_R, PAYMENTS_R, INVENTORY_R, MERCHANT_R`). Add a one-line justification for each
   if asked.
5. **Set the OAuth redirect URL**: in App Settings, set the site/redirect URL to **exactly** your
   app's callback: `https://<your-app-host>/api/v1/pos/clover/callback`. This must match
   `CLOVER_OAUTH_CALLBACK_URL` character-for-character (a mismatch = "no code at callback").
6. **Set the webhook URL**: in App Settings → Webhooks, set the callback to
   `https://<your-app-host>/api/v1/webhooks/pos/clover`. Clover requires HTTPS (no localhost). When
   you save, Clover sends a one-time **verification ping**; our endpoint echoes it back and Clover
   marks the webhook verified. Make sure **order events** are enabled.
7. **Copy your app credentials**: App ID and App Secret (you'll confirm these are set in the app's env
   in Part 2).

Sources: [Create a sandbox app](https://docs.clover.com/dev/docs/creating-a-sandbox-app) ·
[Work with test merchants](https://docs.clover.com/dev/docs/use-test-merchants-dashboard) ·
[Manage app settings](https://docs.clover.com/dev/docs/gdp-manage-app-settings) ·
[Use webhooks](https://docs.clover.com/dev/docs/webhooks).

## PART 2 — Confirm the app's environment (one time)

The deployed app needs these set (DigitalOcean → your app → Settings → Environment):
- `CLOVER_ENVIRONMENT=sandbox`
- `CLOVER_APP_ID`, `CLOVER_APP_SECRET` (from Part 1, step 7)
- `CLOVER_OAUTH_CALLBACK_URL=https://<host>/api/v1/pos/clover/callback` (matches Part 1, step 5)
- `CLOVER_WEBHOOK_AUTH_CODE` (a secret string; Clover sends it back in the `X-Clover-Auth` header — set
  the same value in the Clover webhook config if Clover asks for an auth code/header)
- `TOKEN_ENCRYPTION_KEY` (encrypts stored tokens), `DATABASE_URL`, `SERVICE_DATABASE_URL`
- `WORKOS_CLIENT_ID`, `WORKOS_SECRET_KEY` (so you can get a login token in Part 4)

Set your shell up for the rest of the guide:
```bash
API="https://<your-app-host>"          # e.g. https://reorderos-api-7d4et.ondigitalocean.app
# Database (from DO connection details). Example shape:
PGURL="postgresql://<user>:<pass>@<host>:<port>/<db>?sslmode=require"
alias dbq='psql "$PGURL" -X -c'        # run a one-off query:  dbq "SELECT 1;"
```

---

## PART 3 — Pre-flight: four green checks BEFORE ringing up anything

Do these so that if something's wrong, you find it now — not disguised as "broken ingestion" later.

**3.1 Is the webhook endpoint reachable and echoing?** (proves Clover can reach us)
```bash
curl -sS -X POST "$API/api/v1/webhooks/pos/clover" \
     -H 'Content-Type: application/json' -d '{"verificationCode":"preflight-123"}'
```
✅ It prints `preflight-123`. ❌ Timeout/404/empty → the URL is wrong or the app isn't deployed; fix
before continuing. Also confirm the Clover dashboard shows the webhook **verified**.

**3.2 Start (or confirm) the worker is running.** On DigitalOcean the worker is a separate "Worker"
component — confirm it shows **running** in the app dashboard. To run it yourself instead (pointed at
the same DB), from the `backend/` folder:
```bash
python -m app.workers.inbox_worker      # leave this running in its own terminal; watch the logs
```
✅ Logs show `inbox_worker.starting`. Liveness check (should be 0 now):
```bash
dbq "SELECT count(*) FROM pos_event_inbox WHERE state='pending';"
```

(Checks 3.3 connection and 3.4 item-mapping come AFTER you connect and sync in Parts 4–5; you'll run
them there. The full four-green list lives in the checklist §E.)

---

## PART 4 — Connect the app to your test merchant (the OAuth handshake)

This is the step that was failing ("callback with no code"). Two things to know:
- Our `/connect` endpoint needs your login token in a header — a plain browser can't send that, so we
  use `/connect-url` (returns the link) and you open the link yourself.
- We just fixed a real bug: the link now uses Clover's **v2** authorize endpoint
  (`/oauth/v2/authorize`). The old one returned no usable code.

**4.1 Get your owner login token (JWT):**
```bash
TOKEN=$(curl -sS -X POST "$API/api/v1/auth/sign-in" \
  -H 'Content-Type: application/json' \
  -d '{"email":"OWNER_EMAIL","password":"OWNER_PASSWORD"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
echo "${TOKEN:0:20}…"     # should print the first chars of a long token, not blank
```
❌ `503 Auth not configured` → WorkOS env vars missing. ❌ `401` → wrong password, or this user hasn't
registered a tenant yet.

**4.2 Confirm you're an owner and note your tenant id:**
```bash
curl -sS "$API/api/v1/auth/me" -H "Authorization: Bearer $TOKEN"
```
Find your tenant with `"role":"owner"`; copy its id into `TENANT="..."`.

**4.3 Get the Clover authorize link:**
```bash
curl -sS "$API/api/v1/pos/clover/connect-url" -H "Authorization: Bearer $TOKEN"
# → {"url":"https://sandbox.dev.clover.com/oauth/v2/authorize?client_id=...&redirect_uri=...&state=..."}
```
- Confirm the URL contains **`/oauth/v2/authorize`** (proof the fix is live).
- ❌ `403` tenant/role → add `-H "X-Tenant-Id: $TENANT"` to the command.

**4.4 Open that `url` in a browser**, sign in as your test merchant, and **approve**. Clover sends you
to `…/api/v1/pos/clover/callback?merchant_id=…&code=…&state=…`.
- ✅ The URL has `code=…` → the handshake worked.
- ❌ No `code=…` → see Troubleshooting (redirect_uri mismatch, or you opened the app tile instead of
  this link).

**4.5 Confirm the connection saved:**
```bash
dbq "SELECT merchant_id, state, (access_token_expires_at > now()) AS token_valid
     FROM tenant_pos_connections WHERE vendor='clover' AND tenant_id='$TENANT';"
```
✅ one row, `state='active'`, `token_valid=t`. Copy the `merchant_id`.
(If you got a `code` but `/callback` errored with a 404 on the token exchange, that's the known
"token-host" flag — tell the engineer; it's a one-line fix.)

---

## PART 5 — Make ONE item actually deplete (catalog + recipe)

A sale only moves stock if the item maps to a confirmed recipe.

**5.1 Sync the catalog** (pulls the merchant's items into our `menu_items`):
```bash
curl -sS -X POST "$API/api/v1/pos/clover/sync-menu" -H "Authorization: Bearer $TOKEN"
```
**5.2 Confirm a recipe** for one item you'll sell — do this in the app's Recipes screen (add ≥1
ingredient, confirm). Then verify it's deplete-able:
```bash
dbq "SELECT mi.name, mi.pos_item_id, mi.recipe_version_id, count(ri.*) AS ingredients
     FROM menu_items mi LEFT JOIN recipe_ingredients ri ON ri.recipe_version_id=mi.recipe_version_id
     WHERE mi.tenant_id='$TENANT' AND mi.recipe_version_id IS NOT NULL GROUP BY 1,2,3;"
```
✅ your chosen item has `pos_item_id` + `recipe_version_id` + `ingredients ≥ 1`. (If a recipe unit
differs from the ingredient's storage unit, make sure a conversion exists, or the sale will read
`failed/missing_conversion`.)

---

## PART 6 — Ring up ONE sale and trace it through (the proof)

**6.1** On the **test merchant's Clover register** (sandbox), ring up that mapped item (quantity 1),
take payment with a **test card**, and **close/lock** the order. (Depletion needs the order
`locked` + `paid`.)

Now check each hop in the database. `$TENANT` = your tenant id.

**Hop 1 — Clover has it:** the order appears in the Clover dashboard (Orders). (Not our DB.)

**Hop 2 — it arrived in our inbox (THE arrival proof):**
```bash
dbq "SELECT inbox_id, vendor_object_type, state, signature_verified,
            (fetched_payload IS NULL) AS not_yet_fetched, received_at
     FROM pos_event_inbox WHERE tenant_id='$TENANT' ORDER BY received_at DESC LIMIT 3;"
```
✅ a fresh row: `vendor_object_type='O'`, `signature_verified=t`, `state` `pending`→`processed`,
`not_yet_fetched` flips to `f` once the worker runs. ❌ no row → delivery/auth failed (Troubleshooting).
Copy the newest `inbox_id`.

**Hop 3 — the worker fetched & parsed the REAL order:**
```bash
dbq "SELECT state, processing_error FROM pos_event_inbox WHERE inbox_id='<inbox_id>';"
dbq "SELECT jsonb_pretty(fetched_payload) FROM pos_event_inbox WHERE inbox_id='<inbox_id>';"
```
✅ `state='processed'` and the second command prints the real Clover order JSON. **Read that JSON** —
it's the real shape; this is where you confirm the "§C unknowns" (does it have `item.id` on line
items, `unitQty`, `paymentState`, `refundTotal`, `modifications`?). ❌ stuck `pending` → worker not
running or the fetch failed (check worker logs + Part 4.5 connection active).

**Hop 4 — order + line parsed right:**
```bash
dbq "SELECT clover_line_item_id, menu_item_id, recipe_version_id, quantity,
            depletion_status, depletion_reason
     FROM sale_line_items WHERE tenant_id='$TENANT'
     ORDER BY created_at DESC LIMIT 5;"
```
✅ your line shows `depletion_status='depleted'`, with `menu_item_id` + `recipe_version_id` filled.
❌ `unmapped` → item not mapped (Part 5). ❌ `failed` → read `depletion_reason`.

**Hop 5 — stock actually moved (the payoff):**
```bash
dbq "SELECT inventory_item_id, movement_type, delta
     FROM inventory_movements WHERE source_id='<sale_line_item id from hop 4>';"
```
✅ `sale_depletion` rows with negative `delta` (and/or `sale_signal` positive for count-anchored
items), one per ingredient, sized `quantity × recipe_qty / yield`. Hand-check one against the recipe.

If hops 2→5 all pass for one sale, **the full ingestion path is proven against real Clover** — the
half no test could reach.

## PART 7 — Two safety checks (still one sale)

- **Duplicate delivery:** in Clover, re-save the same order (fires another webhook). Re-run Hop 5 →
  the deltas must be **unchanged** (no doubling).
- **Refund:** refund the sale in Clover → re-run Hop 5 → you should see reversal rows that net the
  ingredient back toward zero, and `sale_line_items.is_refunded` flips true.

---

## Troubleshooting cheat-sheet

| Symptom | Most likely cause | Fix |
|---|---|---|
| 3.1 doesn't echo `preflight-123` | wrong webhook URL / app not deployed | fix the URL; redeploy |
| 4.1 `503 Auth not configured` | WorkOS env vars unset | set `WORKOS_CLIENT_ID/SECRET` |
| 4.1 `401` | wrong password / no tenant yet | check creds; register a tenant first |
| 4.3 URL lacks `/oauth/v2/authorize` | old build deployed | redeploy with the v2 fix (`c1b8720`) |
| 4.4 callback has **no `code`** | redirect_uri ≠ dashboard, or you opened the app tile | match the URL exactly; open the connect-url link, not the app tile |
| 4.4 got `code` but `/callback` 404s | token-host flag (apisandbox vs sandbox host) | tell engineer — one-line fix to `clover_oauth_base_url` |
| Hop 2 no inbox row | delivery failed / `X-Clover-Auth` mismatch / unknown merchant | check webhook config + `CLOVER_WEBHOOK_AUTH_CODE`; confirm Part 4.5 active |
| Hop 3 stuck `pending` | worker down / `get_order` failing | start the worker (3.2); confirm connection active |
| Hop 4 `unmapped` | item not mapped to a confirmed recipe | Part 5 |
| Hop 4 `failed` + `missing_conversion` | recipe unit ≠ storage unit, no conversion | add the conversion |
| Hop 5 delta wrong | real payload shape differs (§C) | capture `fetched_payload`, fix the parser — see checklist §F |

## You're done when…

For one real sandbox sale: pre-flight all green → the sale appears in the inbox → the worker processes
it → the line is `depleted` → the ledger moved by the right amount → duplicate doesn't double → refund
reverses. Save 3-5 real `fetched_payload` JSONs to `backend/tests/fixtures/clover/` so the §C shapes
become permanent tests. **Only then** is the ingestion path proven end-to-end and Sprint 6 unblocked.
