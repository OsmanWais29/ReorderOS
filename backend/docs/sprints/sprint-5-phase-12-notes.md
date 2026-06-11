# Sprint 5 — Phase 12 notes (worker end-to-end: pending lifecycle + line granularity)

> Lighter-weight than an ADR by design. Phase 12 builds **no new behavior** — it proves
> properties the worker restructure (Phases 9/10) already established. These notes record the
> test-design semantics and one diagnostic-shape decision a future reader should not have to
> re-derive. (ADRs stay reserved for decisions that shape the architecture — e.g. ADR 0001's
> ledger-driven reversal. Test-sensitivity and diagnostic-shape calls live here.)
>
> Closes gates 23, 25, 29, 41; fail-gate 13.

## N1 — The processed-vs-failed rule (the boundary between two remediation channels)

This is the rule that decides what the inbox retry mechanism is *for*. State it explicitly so
a future change can't silently violate it.

**Expected terminal line outcomes are DATA states; exceptions are SYSTEM states.**

- A line that ends in a **terminal depletion status** — `failed/missing_conversion`,
  `failed/invalid_recipe`, `failed/sale_ineligible`, `failed/line_refunded`, `unmapped/*`,
  `skipped/*`, or `depleted` — is **not a processing failure**. The worker did its job; the
  job's answer was "this line can't deplete (config incomplete / sale ineligible)." The
  **event is marked `processed`.** Remediation is **operator action** (add the conversion,
  confirm the recipe) surfaced via the **coverage view + diagnostics**, never inbox retry —
  retrying cannot conjure a missing conversion, and the process-once terminal-no-op would
  short-circuit it anyway.
- An **exception** during `process_line` means the worker **did not finish** — the line's
  outcome is unknown (it stays `pending`). The **event is marked `failed`/retryable.** Retry
  is the correct remediation because reprocessing is idempotent and may succeed (transient DB
  error, crash mid-line).

Two clean channels: **coverage/diagnostics for config gaps; inbox retry for system faults.**
A future change that marks events `failed` on a *data* outcome (e.g. `missing_conversion`)
would flood the retry queue with unretryable problems — do not do that.

Worker code: T2 catches only raised exceptions into `any_line_failed`; a terminal return is
not an exception, so the event proceeds to `processed`. Tested both ways (graceful + raised).

## N2 — Line isolation is two guarantees, and crash-recovery is one test

Gate 23 / fail-gate 13 ("one bad line doesn't block others") means different things for the
two failure kinds (N1), so both are tested:

1. **Graceful failure** — a line returns `failed/missing_conversion`; the *other* line still
   depletes; the event is `processed`.
2. **Raised failure (crash mid-line)** — proven as ONE end-to-end test with four assertions
   that together demonstrate *survival AND recovery*:
   - (a) the good line's depletion **persisted** (its own committed transaction);
   - (b) the crashed line is left **observable-`pending`** (not lost);
   - (c) the **event is `failed`/retryable**;
   - (d) on **rerun with the fault removed**, the pending line reprocesses to terminal, and
     **idempotency prevents duplicating** anything the crashed attempt partially wrote.
   Bullet (d) is the point: (a)–(c) prove the crash is survivable; (d) proves the system
   *heals*. That is the full pending-lifecycle story (§39 "worker crash mid-depletion").

## N3 — Historical-pointer test: real un-confirm + a vacuousness guard

Gate 25/41 at runtime: process a real sale against recipe v1, run the **real
`unconfirm_recipe`** operator path, then assert the past sale is untouched.

**The trap (named so it can't regress):** the standard e2e seed helper makes a *separate*
`menu_item` per recipe. Using it here would make the test **silently vacuous** — the sale
would map to a menu_item with no recipe chain, the un-confirm would target an unrelated
recipe, and every assertion would pass trivially because nothing was connected. Green forever,
proving nothing.

Two defenses:
- **Unified hand-seeded chain:** ONE `menu_item` that both carries the `pos_item_id` (so the
  worker maps the sale) AND parents the confirmed `recipes` → `recipe_versions` v1 chain.
- **Precondition / vacuousness guard:** *before* the un-confirm, assert the sale line actually
  depleted against v1 (movements exist with v1 in their idempotency keys). If the setup ever
  regresses to the disconnected helper, this fails **loudly** instead of the test passing
  emptily.

Assertions after the real un-confirm: sale line still points at v1; `recipe_versions` v1 +
its `recipe_ingredients` byte-identical; ledger movements unchanged; `menu_items.recipe_version_id`
cleared; `recipes.status='draft'`; and the produced draft is **correctly parented**
(`recipe_drafts.parent_recipe_version_id = v1`) — proving a valid un-confirm, not just a
cleared pointer.

## N4 — Stuck-pending diagnostic: shape, definition, and timestamp

Gate 29 / operational-concern 5. The deliverable is a **read-only diagnostic query**, not a
read endpoint (consistent with F.5 — no new read endpoints in Sprint 5; this is an ops alert).

- **Unprocessed = `depletion_status IS NULL OR = 'pending'`** — the SAME definition the
  handler's process-once gate uses. A NULL-status line (pre-cutover ingest) is exactly as
  stuck as a `pending` one; if the detector and the gate disagreed, they'd disagree about what
  "stuck" means.
- **Anchor timestamp = `created_at`, NOT `recorded_at`.** The diagnostic measures
  *ingested-but-not-processed for N minutes* — a **worker-health** signal. `recorded_at` (the
  POS business-event time) would conflate worker lag with webhook **delivery** lag: a sale
  recorded two hours ago but ingested one minute ago is not stuck — the worker just received
  it. The recorded-vs-now gap is already the **late-signal monitor's** job. Two different
  signals, two different timestamps; do not conflate them.
- **Caller / role:** `stuck_pending_lines` is worker/ops self-reporting; it reads
  `sale_line_items`, on which `service_worker` already holds SELECT (migration 0006). **No new
  grant** — verified, not assumed (the keystone reflex). If a future external ops tool calls
  it under a different role, that role's SELECT is a separate concern outside the worker grant.
