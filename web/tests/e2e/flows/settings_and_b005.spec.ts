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

  // Type a secret value into the GEMINI_API_KEY row and save.
  const geminiRow = page.locator('div', { hasText: /^🟢 GEMINI_API_KEY|⚪ GEMINI_API_KEY/ }).first();
  const input = geminiRow.locator('input[type="password"]').first();
  await input.fill('test-key-from-e2e');
  await geminiRow.getByRole('button', { name: 'Save' }).click();

  // The status must flip to "set" (🟢 prefix) within a couple seconds
  // of the PUT round-trip.
  await expect(page.locator('text=/🟢\\s+GEMINI_API_KEY/')).toBeVisible({
    timeout: 5_000,
  });
});

test('B-005: composite op shows "(composite)" not "—" in cost preview', async ({ page }) => {
  await page.goto(`${baseURL}/ui/run`);
  // Pick intelligence.summarize from the op picker.
  await page.locator('select').first().selectOption('intelligence.summarize');
  // Wait for the debounced cost preview to fire (~250ms) + render.
  // The fix surfaces "(composite — chosen at run time)" via the
  // `embedded` flag from /run/preview. The pre-fix UI would show "—".
  await expect(page.getByText('(composite — chosen at run time)')).toBeVisible({
    timeout: 5_000,
  });
});
