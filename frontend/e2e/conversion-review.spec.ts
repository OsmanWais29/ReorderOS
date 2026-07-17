// Conversion-review regression (live-cert fix): an ea → L line must block
// receiving with a named, UUID-free explanation, offer the evidence-backed
// suggestion, and only enable Receive after acceptance + re-affirmation.
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { test, expect, type Page } from '@playwright/test';

const API = 'http://localhost:8123';
const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

let token: string;
let tenantId: string;
let receiptId: string;
let promoLineId: string;

test.beforeAll(async ({ request }) => {
  // Dev sign-in (double-gated backend path; local only) → token + tenant.
  const signIn = await request.post(`${API}/api/v1/auth/dev-sign-in`, {
    data: { email: `e2e-${Date.now()}@test.local` },
  });
  expect(signIn.ok()).toBeTruthy();
  token = (await signIn.json()).access_token;
  const me = await request.get(`${API}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  tenantId = (await me.json()).tenants[0].id;

  // Seed the scenario straight into the local test DB (python, asyncpg).
  const seeded = seed(tenantId);
  receiptId = seeded.receipt_id;
  promoLineId = seeded.promo_line_id;
});

function seed(tid: string): { receipt_id: string; promo_line_id: string } {
  const out = execFileSync(
    path.join(__dirname, '..', '..', 'backend', '.venv', 'bin', 'python'),
    [path.join(__dirname, 'seed_conversion_draft.py'), tid],
    { encoding: 'utf-8' },
  );
  return JSON.parse(out.trim());
}

async function openReview(page: Page): Promise<void> {
  await page.addInitScript((t) => window.localStorage.setItem('auth_token', t), token);
  await page.goto(`/receipt/${receiptId}`);
  await expect(page.getByText('SIROP VANILLE 750ML')).toBeVisible();
}

test('blocked ea→L line: named, marked, disabled CTA; accept+re-affirm enables', async ({
  page,
}) => {
  await openReview(page);

  // ── The syrup line is marked "Needs attention"; SIROP VANILLE is NAMED ──
  await expect(page.getByText('Needs attention').first()).toBeVisible();
  await expect(
    page.getByText('The invoice uses ea, but SIROP VANILLE is tracked in L.'),
  ).toBeVisible();

  // ── No UUID anywhere in the visible review UI ──
  const bodyText = (await page.locator('body').innerText()).replace(receiptId, '');
  expect(bodyText).not.toMatch(UUID_RE);

  // ── Atomic-truth CTA: 1 of 2 ready; TWO issues block (conversion + the
  //    promo discount's undecided disposition) ──
  await expect(page.getByText('1 of 2 items ready')).toBeVisible();
  const blockedCta = page.getByText('Resolve 2 issue(s) to receive 2 items');
  await expect(blockedCta).toBeVisible();

  // ── Clicking the blocker row scrolls the syrup line into view ──
  await page.getByText('1 quantity(ies) need confirmation').click();
  await expect(page.getByText('SIROP VANILLE 750ML')).toBeInViewport();

  // ── The evidence-backed suggestion is shown (750 ml → 0.75 → 4.5 L) ──
  await expect(page.getByText('ReorderOS will receive: 4.5 L')).toBeVisible();

  // Affirm FIRST so we can prove acceptance clears it.
  const affirm = page.getByRole('switch').first();
  await affirm.click();
  await expect(affirm).toBeChecked();

  // ── Accept 1 ea = 0.75 L ──
  await page.getByText('Accept suggestion').click();

  // 6 ea became 4.5 L (confirmed summary), and the affirmation was CLEARED.
  await expect(page.getByText('ReorderOS will receive: 4.5 L → SIROP VANILLE')).toBeVisible();
  await expect(page.getByText('How: 6 ea × 0.75 L = 4.5 L')).toBeVisible();
  await expect(page.getByRole('switch').first()).not.toBeChecked();

  // ── Decision gate: the conversion is fixed but the discount still needs a
  //    decision — CTA stays blocked, and the blocker OPENS the section ──
  await expect(page.getByText('Resolve 1 issue(s) to receive 2 items')).toBeVisible();
  await page.getByText('1 adjustment(s) need a decision').click();
  await expect(page.getByText('PROMO SIROP -10%')).toBeVisible();
  await expect(page.getByText('Needs decision')).toBeVisible();

  // ── Part C: link the promo discount → net-cost breakdown before approval ──
  await page.getByText('Apply to item…').click();
  await page.getByText('SIROP VANILLE', { exact: true }).click(); // picker chip
  await expect(page.getByText('Applied to SIROP VANILLE')).toBeVisible();
  // Gross $53.70 · −$5.37 · Net $48.33 → $10.74/L — the exact commit basis.
  await expect(
    page.getByText('Gross: $53.70 · Adjustments: −$5.37 · Net: $48.33'),
  ).toBeVisible();
  await expect(page.getByText('Cost: $48.33 ÷ 4.5 L = $10.74/L')).toBeVisible();

  // ── Functional disabled-proof: without re-affirmation, clicking does NOTHING ──
  await expect(page.getByText('2 of 2 items ready')).toBeVisible();
  const cta = page.getByText('Receive 2 items into stock');
  await expect(cta).toBeVisible();
  await cta.click();
  await page.waitForTimeout(1500);
  await expect(page.getByText('Inventory received')).toHaveCount(0);

  // ── Re-affirm → Receive enables → the receipt actually receives ──
  await page.getByRole('switch').first().click();
  await cta.click();
  await expect(page.getByText('Inventory received')).toBeVisible();
  await expect(page.getByText('+4.5 L')).toBeVisible(); // 6 ea became 4.5 L, received
  await expect(page.getByText('$10.74/L')).toBeVisible(); // NET unit cost, not gross
});

test('adjustment decisions are truthful: failed link never shows Applied; keep-separate confirms and persists', async ({
  page,
}) => {
  // Fresh receipt — the first test received the shared one.
  const seeded = seed(tenantId);
  receiptId = seeded.receipt_id;
  promoLineId = seeded.promo_line_id;
  await openReview(page);
  await page.getByText('1 adjustment(s) need a decision').click();
  await expect(page.getByText('Needs decision')).toBeVisible();

  // ── Truthful failure (the Gate-1 lesson): if the link PUT never lands, the
  //    UI must keep saying "Needs decision" — no optimistic "Applied to" ──
  await page.route(`**/lines/${promoLineId}`, (route) => route.abort());
  await page.getByText('Apply to item…').click();
  await page.getByText('SIROP VANILLE', { exact: true }).click();
  await expect(page.getByText('Couldn’t save — try again.')).toBeVisible();
  await expect(page.getByText('Applied to SIROP VANILLE')).toHaveCount(0);
  await expect(page.getByText('Needs decision')).toBeVisible();
  await expect(page.getByText('1 adjustment(s) need a decision')).toBeVisible();
  await page.unroute(`**/lines/${promoLineId}`);

  // ── Keep separate: explicit confirmation states the money consequence ──
  await page.getByText('Keep separate', { exact: true }).click();
  await expect(
    page.getByText('−$5.37 discount will not be included in inventory cost.'),
  ).toBeVisible();
  await page.getByText('Keep separate', { exact: true }).click(); // confirm
  await expect(page.getByText('Kept separate from inventory cost')).toBeVisible();
  await expect(page.getByText('1 adjustment(s) need a decision')).toHaveCount(0);

  // ── Persistence: a full reload renders the SERVER state, not memory ──
  await page.reload();
  await expect(page.getByText('SIROP VANILLE 750ML')).toBeVisible();
  await page.getByText(/^Not added to stock/).click();
  await expect(page.getByText('Kept separate from inventory cost')).toBeVisible();

  // ── Change decision → reopens; then a REAL link persists across reload ──
  await page.getByText('Change decision').click();
  await expect(page.getByText('Needs decision')).toBeVisible();
  await page.getByText('Apply to item…').click();
  await page.getByText('SIROP VANILLE', { exact: true }).click();
  await expect(page.getByText('Applied to SIROP VANILLE')).toBeVisible();
  await page.reload();
  await expect(page.getByText('SIROP VANILLE 750ML')).toBeVisible();
  await page.getByText(/^Not added to stock/).click();
  await expect(page.getByText('Applied to SIROP VANILLE')).toBeVisible();
});
