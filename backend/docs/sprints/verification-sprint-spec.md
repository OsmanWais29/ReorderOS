# Verification Sprint — Spec (V1–V8 + Operational Readiness)

> **Status: LOCKED (2026-06-12).** Structured from the founder's verification-sprint relay,
> reviewed section-by-section against source, ownership re-grounded against the failure-catalog
> sprint headers + `SPRINTS.md`, and locked. This is the canonical document for the verification
> sprint; all "what does V-sprint require?" questions resolve here. Same spec-before-execution
> discipline as Sprint 5.

## Purpose

Prove Sprints 1–5 **deployment-ready** via evidence chains, and produce **restaurant-facing
proof artifacts**. **Audit-first:** extend the existing failure catalog, `diagnostics.py`, the
coverage view, and the Phase 15 matrix — never duplicate them.

## Global rules (apply to every phase)

- **Read-first** per phase before any assertion.
- **Would-it-fail standard** for every coverage citation, typed **U** (pure unit) / **I** (DB
  integration) / **E** (real-role e2e) / **S** (static) / **M** (migration) / **P** (process) /
  **D** (diagnostic).
- **Negative properties** require a **prevention-test** or **structural-mechanism** as evidence —
  "doesn't occur in our tests" is vacuous.
- **Any privilege-dependent invariant tested only as superuser = PARTIAL by rule** — whichever
  sprint owns it. The rule is about privilege dependence, never a sprint number (the Slice B
  privilege bugs are the justification; a numeric form would mis-exempt a superuser-only ledger
  test once sprints are renumbered).
- **Every partial/gap carries a disposition:** `fill-now` / `accept-with-rationale` / `tracked`.
- **Each phase's matrix comes to review BEFORE gap-filling.**
- **Mode B is first-class throughout.**
- **Expected shape is NOT all-green** — all-green is itself a red flag.
- **Fidelity checks paste machine-extracted text, never retyped quotes** — the L22 "superhuman"
  incident: a retyped "verbatim" quote corrupted the rule's load-bearing word ("superuser")
  while certifying it faithful. Quote from the file, not from memory.

---

## V1 — Five-sprint failure-class audit

Per sprint, **surface-inventory read-first → invariants → the eleven failure classes → classify
all ~590 tests → evidence-kind → dispositions**. Sprint→surface ownership is grounded in the
failure-catalog sprint headers + `SPRINTS.md` (NOT the relay's concern-grouping, which inverted
Sprint 3/4 and omitted Sprint 1):
- **Sprint 1 — Platform Skeleton:** deploy / health (F1.1), migrations-at-deploy (F1.2), app
  config / env / CORS. *(Relay omitted this sprint; it lands here.)*
- **Sprint 2 — Tenant, Auth & RBAC:** auth / tenancy / RLS / roles / invitations / team.
- **Sprint 3 — Inventory Ledger Core:** units / inventory-items; the ledger — append-only,
  reversals, yields, counts, watermark, `on_hand`, and the Mode B accounting math.
- **Sprint 4 — Clover Integration MVP:** Clover OAuth / webhook / inbox / reconciliation.
- **Sprint 5 — Recipe Walk & Sale Depletion:** cite the Phase 15 matrix; audit only the
  **seams** (don't re-audit closed gates).

**The eleven failure classes:** (1) duplicate input → double-writes; (2) retry changes result;
(3) crash leaves partial state; (4) stale data used; (5) cross-tenant leak; (6) lower role acts;
(7) invalid config passes silently; (8) external-API failure corrupts state; (9) migration
apply/rollback fails; (10) history changes after edits; (11) UI shows success when backend did
nothing.

**Priority reads, in order:**
1. **Webhook signature — DONE** (findings recorded below as **F4 rows** — webhook is Sprint 4 /
   Clover in the catalog namespace).
2. **Append-only enforcement on `inventory_movements`** — structural (revoked UPDATE/DELETE) or
   convention?
3. **Mode B observed-consumption formula sweep** — grep every code AND doc site for
   `previous_count − current_count − waste`, hunting the **receipts-between error class**.
4. **Mixed-mode-same-line** and **Mode B modifier/refund** test existence.
5. **Negative observed-consumption** behavior (likely unassigned — a minimum-fill diagnostic
   surfaces it).

**Permanent sweeps, delivered as tests:**
- **RLS sweep** — `pg_policies` vs a full enumeration of tenant tables (every tenant table has a
  policy).
- **Grant matrix** — live grants vs a committed **intended-grants** file.
- **Migration round-trip** — `0001 → head → base → head` clean.
- **Superuser-only test census** — enumerate privilege-dependent tests lacking a real-role e2e.

**Sprint specifics (by true owner):**
- **Sprint 1 (Platform Skeleton):** migration apply/rollback at deploy (failure class 9 —
  structural: `validate_schema_head` RuntimeError guard at lifespan startup); **config validation
  (class 7) — PARTIAL → fill-now:** `get_settings()` raises `RuntimeError` on a pydantic
  ValidationError (covers genuinely-required fields + the `database_url` validator), BUT
  security-critical secrets (`token_encryption_key`, `clover_webhook_auth_code`, WorkOS fields)
  default to `None` and the app boots silently without them — the class-7 hazard. Fill: a
  production fail-closed assertion (required when `app_env == production`). CORS posture.
- **Sprint 2 (Auth & RBAC):** endpoint × role matrix (role-below → 403, cross-tenant → 404);
  GUC-absent fails closed; invitation `FOR UPDATE` race; WorkOS JWKS/env coupling.
- **Sprint 3 (Inventory Ledger) / Mode B:** `unit_type` integrity on legacy creation paths — do
  pre-0019 item-creation paths handle the normalization index gracefully (409) or 500?
  `storage_to_recipe_factor` consumer sweep verifying the vestigial-for-depletion claim
  codebase-wide; append-only enforcement; reversal linkage + `yield_factor_applied` carry;
  `on_hand = SUM(ledger)` with **zero materialized readers**; sale → `sale_signal` positive,
  never `sale_depletion`; **mixed-mode SAME LINE** (one line, both modes → one depletion + one
  signal — the sensitivity condition separate-line tests can't see); Mode B modifier;
  `sale_signal_reversal` e2e; `SUM(delta × yield_factor_applied)` never `SUM(ABS)`; mode-qualified
  yield per §10; count = hard reset, boundary attribution, count-replay idempotency; late-signal
  covers Mode B signals (confirm as decided, not defaulted).
- **Sprint 4 (Clover Integration):** inbox event idempotency; out-of-order **refund-before-sale**
  actual-vs-documented behavior; pagination partial-failure vs watermark; token-refresh failure
  mid-sync.

**Webhook-auth findings → F4 rows (Sprint 4 / Clover; from the signature read, 2026-06-12),
extending the existing F4.1–F4.3:**
- **F4.4 webhook auth — static shared secret:** event payloads require `X-Clover-Auth ==
  settings.clover_webhook_auth_code`, compared with `hmac.compare_digest` (constant-time);
  forge-without-secret → 401 before any inbox write. **COVERED-with-caveats.** Caveat: it's a
  static shared secret, NOT a payload-HMAC (no body binding). **Hardening (payload-HMAC) is
  constrained by Clover's model — you can't verify signatures Clover doesn't generate; ground
  availability in V7; accept-with-rationale if unsupported.**
- **F4.5 webhook auth — single global secret, cross-tenant scope:** one global secret guards all
  tenants (merchant→tenant resolved AFTER auth via `lookup_tenant_by_merchant`). **Per-merchant
  secret availability → V7** (Clover webhooks are per-app; a per-tenant code likely doesn't exist
  → accept-with-rationale if so). **fill-now:** verify `lookup_tenant_by_merchant` **fails
  closed** on an unknown `merchant_id` (dropped, never auto-created) — one read, one test.
- **F4.6 webhook auth — no replay protection:** no timestamp/nonce; a captured valid request can
  be replayed. **Note** — mitigated by proven downstream inbox/worker idempotency (replay won't
  double-deplete).
- The genuinely available, **Clover-independent** hardening for the static-secret residual risk
  is the **bidirectional divergence monitor → V8b** (webhook-delivered-but-poller-can't-find =
  forgery detector; a leaked-secret injection is an orphan within one reconciliation cycle).
- **Secret-rotation runbook → operational layer** (rotatability is the static secret's main
  mitigation).

**Deliverable:** per-sprint matrices in Phase 15 format + failure-catalog entries in the
existing **dotted `F{sprint}.{n}`** namespace (e.g. `F4.4`), continuing each sprint's sequence —
ONE namespace, no second scheme (YAML: `id, sprint, invariant, failure, test, monitor, severity,
status`). **Matrix to review before fills.**

---

## V2 — Reference depletion model

`tests/reference_models/depletion.py`, derived from `inventory_accounting_semantics.md` **ONLY**
— **never read the production walker/writer while writing it**; cite the doc section for each
formula. Covers **Mode A AND Mode B** (including observed consumption). **Differential tests:**
real-pipeline output == reference output. Doc ambiguities are **flagged as doc bugs**, never
silently resolved.

---

## V3 — Property-based + metamorphic

**Hypothesis** with **pinned, reported seeds**; randomized quantities / yields / conversion
factors / UUIDs, all compared against the **V2 reference**.
- **Relations (Mode A):** double-qty ⇒ double-depletion; split-sale preserves totals;
  ingredient-order irrelevance; conversion-factor sensitivity; `storage_to_recipe_factor`
  insensitivity (cite the existing test); replay invariance; refund nets to zero.
- **Relations (Mode B):** waste `W` shifts sales-attributed consumption by exactly `W` under
  fixed counts; a count resets the anchor regardless of accumulated signals; count replay changes
  nothing; doubling Mode B quantity doubles the signal.

---

## V4 — Monitors as tested diagnostics

Add **duplicate-idempotency-key detector** and **coverage-collapse** to `diagnostics.py` beside
the existing two; **every monitor sensitivity-tested by seeding the bad state it detects.** Ops
runbook: monitor → meaning → remediation. **Pinned SQL in docs is not a mechanism — functions
with tests only.**

---

## V5 — Restaurant simulation harness

Synthetic mixed-mode restaurant (menu, confirmed recipes + modifiers, Mode A and B items); a
**seeded, deterministic 7-day traffic generator** — sales, modifier sales, refunds, partial
refunds, voids, duplicate webhooks, out-of-order arrivals, unmapped items, count events, waste,
at realistic configurable proportions — driven through the **REAL inbox→worker→ledger as
`service_worker`**.

**Audit report:** ledger vs reference per ingredient; the **Mode B triangle** (generator-truth
vs signal-sum vs count-derived observed consumption — all three reconciled); all monitors clean;
coverage %; every failed reason explained. A **restaurant-facing artifact that states its own
limits** (proves the pipeline under synthetic traffic; sandbox certification and pilot monitors
are the remaining rungs). Doubles as a **load-sanity check** at compressed wall-clock.

---

## V6 — Unified traceability

The Phase 15 matrix shape **extended to all five sprints**: failure class → test(s) → monitor →
manual proof. **One document**; every claimed protection shows its chain.

---

## V7 — Clover sandbox certification

Runbook against a **real sandbox merchant**: install, OAuth, webhook verification handshake, then
real POS actions (sale / modifier sale / refund / partial refund / void), **each paired with an
automated post-step DB assertion script** (webhook received + auth valid + inbox row + ledger
rows match reference). **Capture real payload bodies; diff against ALL fixtures** — divergences
are **fixture bugs, fix them.** **Ground the webhook hardening questions in Clover's actual
model** (payload-HMAC availability, per-merchant secrets); record provider-constrained hardenings
as **accept-with-rationale**. Output: a **certification report** with recorded evidence.
*(Operator actions are Vito's; assertions are scripted.)*

---

## V8 — Connection health, productized

- **(a) Per-merchant onboarding verification:** handshake flag, catalog-sync success,
  `first_webhook_received_at`, the operator **"ring up one sale"** step.
- **(b) Continuous monitors, sensitivity-tested:** **webhook liveness** (`last_webhook_event_at`
  vs reconciliation-visible activity) and **divergence in BOTH directions** —
  poller-found-but-webhook-missing (**delivery failure**) AND webhook-delivered-but-poller-can't-find
  (**injection / spuriousness — the forgery detector that compensates for static-secret auth**).
- **(c) Dual-path idempotency test:** the same order via webhook **and** poller processes exactly
  once.
- **Degradation story documented:** webhooks dead = 15-minute latency via reconciliation, never
  lost sales.

---

## Operational-readiness layer (with / after V8)

- Monitor scheduling + **alert delivery** (channel is Vito's decision).
- **Backup/restore drill** — actually restore to a scratch instance and run the app against it,
  with a runbook.
- **Environment config audit** — WorkOS JWKS/env coupling explicitly, secrets, CORS, production
  roles match the grant matrix.
- **Rollback runbook** — DO deploy history + tested downgrades.
- **Webhook secret rotation procedure.**
- **Final deliverable — the Deployment Readiness Review (DRR):** every readiness claim mapped to
  its artifact, gaps dispositioned, Phase 15 discipline.

---

## Sequencing

**V1 matrix → review → fills → V2 → V3 → V4 → V5 → V6**; **V7** slots when the sandbox session is
available; **V8 + ops layer**; **DRR last.**

**External calendar items (founder, not agent):**
- **FE checklist run** — independent, **first** (cheapest; closes the Phase 15 matrix's two
  PARTIALs).
- **Clover sandbox session** (V7).
- **Alert channel decision** (operational layer).
- **Backup drill** (operational layer).
- **Francophone review** — pre-Québec (Bill 96 legacy migration).
