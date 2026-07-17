// Conversion-review regression (live-cert fix): an ea → L line must block
// receiving with a named, UUID-free explanation, offer the evidence-backed
// suggestion, and only enable Receive after acceptance + re-affirmation.
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { test, expect, type Page } from '@playwright/test';

const API = 'http://localhost:8123';
const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

let token: string;
let receiptId: string;

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
  const tenantId = (await me.json()).tenants[0].id;

  // Seed the scenario straight into the local test DB (python, asyncpg).
  const out = execFileSync(
    path.join(__dirname, '..', '..', 'backend', '.venv', 'bin', 'python'),
    [path.join(__dirname, 'seed_conversion_draft.py'), tenantId],
    { encoding: 'utf-8' },
  );
  receiptId = JSON.parse(out.trim()).receipt_id;
});

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

  // ── Atomic-truth CTA: 1 of 2 ready, disabled "Resolve 1 issue…" ──
  await expect(page.getByText('1 of 2 items ready')).toBeVisible();
  const blockedCta = page.getByText('Resolve 1 issue(s) to receive 2 items');
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
});
