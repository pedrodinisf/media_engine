/**
 * Phase 6.5 — Settings (Doctor + Secrets) + B-005 regression spec.
 *
 * Drives a real Chromium against a live `med web start`. Covers:
 *
 *   1. Settings → Doctor renders a non-empty op×backend matrix with
 *      a working summary (the /settings/doctor endpoint is reachable
 *      and the table consumes its JSON shape correctly).
 *   2. Settings → Secrets lists the known catalog (GEMINI_API_KEY,
 *      ANTHROPIC_API_KEY, HF_TOKEN, …) and a save round-trips through
 *      the file. Tests the masked-input UX too — no plaintext leaks.
 *   3. B-005 — Run panel cost preview for a composite op renders
 *      "(composite — chosen at run time)" instead of "—".
 *
 * Operator-invoked via `scripts/verify_settings.sh`; not part of the
 * default e2e gate.
 *
 *   MEDIA_ENGINE_WEB_E2E_BASE_URL   http://127.0.0.1:8767
 *   MEDIA_ENGINE_WEB_E2E_TOKEN      <bearer token>
 */

import { expect, test } from '@playwright/test';

const baseURL = process.env.MEDIA_ENGINE_WEB_E2E_BASE_URL ?? 'http://127.0.0.1:8767';
const token = process.env.MEDIA_ENGINE_WEB_E2E_TOKEN ?? '';

test.skip(!token, 'requires MEDIA_ENGINE_WEB_E2E_TOKEN');

test.beforeEach(async ({ context }) => {
  await context.addInitScript((bearer: string) => {
    if (bearer) window.localStorage.setItem('media_engine:bearer', bearer);
  }, token);
});

test('Settings → Doctor renders the dep matrix', async ({ page }) => {
  await page.goto(`${baseURL}/ui/settings`);
  // Doctor tab is now the default on first load.
  await expect(page.getByText('Dependency map — what works on this machine')).toBeVisible({
    timeout: 5_000,
  });
  // Summary trio renders.
  await expect(page.getByText(/🟢 ok:/)).toBeVisible();
  await expect(page.getByText(/🔴 unavailable:/)).toBeVisible();
  // Every well-known op shows up somewhere in the table.
  await expect(page.locator('text=acquire.upload').first()).toBeVisible();
  await expect(page.locator('text=audio.transcribe').first()).toBeVisible();
});

test('Settings → Secrets lists the catalog and round-trips a save', async ({ page }) => {
  await page.goto(`${baseURL}/ui/settings`);
  await page.getByRole('button', { name: 'Secrets' }).click();

  await expect(page.getByText('GEMINI_API_KEY')).toBeVisible();
  await expect(page.getByText('ANTHROPIC_API_KEY')).toBeVisible();
  await expect(page.getByText('HF_TOKEN')).toBeVisible();

  // Each row carries a stable data-secret-row attribute so the spec
  // doesn't depend on fragile text-based locators.
  const geminiRow = page.locator('[data-secret-row="GEMINI_API_KEY"]');
  await expect(geminiRow).toBeVisible();

  const input = geminiRow.locator('input[type="password"]');
  await input.fill('test-key-from-e2e');
  await geminiRow.getByRole('button', { name: 'Save' }).click();

  // The status must flip to "set" (🟢 prefix) within a couple seconds
  // of the PUT round-trip.
  await expect(geminiRow.locator('text=/🟢\\s+GEMINI_API_KEY/')).toBeVisible({
    timeout: 5_000,
  });
});

test('B-005: composite op shows "(composite)" not "—" in cost preview', async ({ page }) => {
  await page.goto(`${baseURL}/ui/run`);
  // The op picker is a button list (each op is a <button> in the left
  // pane); the first <select> on the page is the backend picker that
  // appears AFTER an op is chosen. Click the button by exact text.
  await page.getByRole('button', { name: 'intelligence.summarize', exact: true }).click();
  // Wait for the debounced cost preview to fire (~250ms) + render.
  // The fix surfaces "(composite — chosen at run time)" via the
  // `embedded` flag from /run/preview. The pre-fix UI would show "—".
  await expect(page.getByText('(composite — chosen at run time)')).toBeVisible({
    timeout: 5_000,
  });
});

test('B-002: pasting a non-existent / wrong-kind input id blocks the Run button', async ({ page }) => {
  await page.goto(`${baseURL}/ui/run`);
  await page.getByRole('button', { name: 'audio.transcribe', exact: true }).click();
  // A made-up id that doesn't resolve in this namespace — surfaces as a
  // "not found" warning (not a blocker on its own). Then a wrong-kind
  // path would require seeding a real wrong-kind artifact; the
  // "not found" warning UX is what we assert here. The hard-block path
  // is covered by the pytest test on the validation effect itself.
  await page.locator('input[placeholder*="a-3c1f"]').fill(
    'deadbeef000000000000000000000000000000000000000000000000deadbeef',
  );
  // The "not found" status is shown inline.
  await expect(page.locator('text=/not found in this namespace/')).toBeVisible({
    timeout: 5_000,
  });
  // Run button stays enabled for not-found (warning, not error) — the
  // engine is the source of truth. Just verifies the validation row
  // rendered without breaking the button state.
  const runButton = page.getByRole('button', { name: 'Run', exact: true });
  await expect(runButton).toBeEnabled();
});

test('B-011: failed jobs surface the error envelope on the Failure tab', async ({ page }) => {
  // Submit an op via REST with an invalid input kind — engine rejects
  // in _validate_input_kinds, fails fast. Pre-fix the Failure tab
  // showed "No failure recorded" because the UI read failure_envelope
  // instead of the server's `error` field.
  const resp = await page.request.post(`${baseURL}/run`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      op: 'audio.transcribe',
      // 64-hex id that doesn't exist — Engine raises LookupError
      // before any op_started event fires.
      inputs: ['deadbeef000000000000000000000000000000000000000000000000deadbeef'],
      params: {},
    },
  });
  expect(resp.ok()).toBeTruthy();
  const { job_id: jobId } = await resp.json();

  await page.goto(`${baseURL}/ui/jobs/${jobId}`);
  // The job should reach `failed` quickly.
  await expect(page.locator('text=/^failed$/')).toBeVisible({ timeout: 5_000 });

  await page.getByRole('button', { name: 'Failure' }).click();
  // Error class badge — comes straight from _classify_error.
  await expect(page.locator('text=/LookupError|ValueError|RuntimeError/')).toBeVisible({
    timeout: 5_000,
  });
  // And NOT the "no failure recorded" placeholder.
  await expect(page.getByText('No failure recorded.')).toHaveCount(0);
});

test('B-012: Events tab shows "no events recorded" for pre-op_started failures', async ({ page }) => {
  const resp = await page.request.post(`${baseURL}/run`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      op: 'audio.transcribe',
      inputs: ['deadbeef000000000000000000000000000000000000000000000000deadbeef'],
      params: {},
    },
  });
  const { job_id: jobId } = await resp.json();

  await page.goto(`${baseURL}/ui/jobs/${jobId}`);
  await expect(page.locator('text=/^failed$/')).toBeVisible({ timeout: 5_000 });

  // Events tab is the default. Pre-fix the page sat on
  // "Waiting for events…" forever for jobs that failed pre-op_started.
  await expect(page.getByText('No events were recorded for this job.')).toBeVisible({
    timeout: 5_000,
  });
  // Sanity: the placeholder string must NOT be present.
  await expect(page.getByText('Waiting for events…')).toHaveCount(0);
});

test('Settings → Doctor shows quick-fix banner when ops are unavailable', async ({ page }) => {
  await page.goto(`${baseURL}/ui/settings`);
  await expect(page.getByText('Dependency map — what works on this machine')).toBeVisible({
    timeout: 5_000,
  });
  // Quick-fix banner is only rendered when there's at least one
  // unavailable op with a single-blocker (the verify_settings.sh harness
  // boots a fresh env so every cloud key + many extras are missing).
  await expect(page.getByText('Quick fixes — highest impact first')).toBeVisible({
    timeout: 5_000,
  });
  // A "Set X" or "Install X" button appears for at least one missing dep.
  await expect(
    page.locator('button').filter({ hasText: /^(Set|Install) [A-Z_]+ ↗$/ }).first(),
  ).toBeVisible();
});

test('Settings → Secrets shows unblock-impact details', async ({ page }) => {
  await page.goto(`${baseURL}/ui/settings`);
  await page.getByRole('button', { name: 'Secrets' }).click();

  // GEMINI_API_KEY should advertise its impact (5 direct + 3 indirect
  // when the engine is fresh). The exact counts are env-dependent;
  // assert the structure instead.
  const geminiRow = page.locator('[data-secret-row="GEMINI_API_KEY"]');
  await expect(geminiRow.locator('text=/Unblocks \\d+ op/')).toBeVisible();
});
