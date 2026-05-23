/**
 * Phase 6.5 — B-001 regression spec.
 *
 * Drives a real Chromium against a live `med web start` instance,
 * submits a job through the REST API, navigates to the Job detail
 * page, and asserts that the Events tab populates within 5 seconds.
 *
 * Before the fix, the events list stayed on "Waiting for events…"
 * indefinitely (three root causes: Engine.run minted its own id,
 * client listened for PascalCase event names while server emits
 * snake_case, and the subscribe-after-emit race dropped events
 * fired before the EventSource handshake completed). This spec
 * gates against all three regressing simultaneously.
 *
 * Environment contract (set by the harness wrapper):
 *
 *   MEDIA_ENGINE_WEB_E2E_BASE_URL   http://127.0.0.1:8767
 *   MEDIA_ENGINE_WEB_E2E_TOKEN      <bearer token>
 *   MEDIA_ENGINE_WEB_E2E_FIXTURE    path to a small mp4 fixture
 *
 * Not part of the default e2e gate; the harness is operator-invoked.
 */

import { expect, test } from '@playwright/test';

const baseURL = process.env.MEDIA_ENGINE_WEB_E2E_BASE_URL ?? 'http://127.0.0.1:8767';
const token = process.env.MEDIA_ENGINE_WEB_E2E_TOKEN ?? '';
const fixturePath = process.env.MEDIA_ENGINE_WEB_E2E_FIXTURE ?? '';

test.skip(!token, 'requires MEDIA_ENGINE_WEB_E2E_TOKEN');
test.skip(!fixturePath, 'requires MEDIA_ENGINE_WEB_E2E_FIXTURE');

test.beforeEach(async ({ context }) => {
  await context.addInitScript((bearer: string) => {
    if (bearer) window.localStorage.setItem('media_engine:bearer', bearer);
  }, token);
});

test('B-001: job detail Events tab populates within 5s of submission', async ({ page }) => {
  // Submit a job through REST so we don't depend on the Ingest UI
  // (which itself does a POST /run via the form). This isolates the
  // SSE delivery path.
  const resp = await page.request.post(`${baseURL}/run`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      op: 'acquire.upload',
      inputs: [],
      params: { source_path: fixturePath, link_mode: 'copy' },
    },
  });
  expect(resp.ok()).toBeTruthy();
  const { job_id: jobId } = await resp.json();
  expect(jobId).toBeTruthy();

  // Navigate to the job detail page; the $effect fires immediately and
  // opens the SSE stream.
  await page.goto(`${baseURL}/ui/jobs/${jobId}`);

  // Pre-fix: this assertion would time out (events.length stays 0).
  // Post-fix: replay delivers any persisted events for this job
  // immediately, so the list populates within ~1s of page load.
  const eventsList = page.locator('ul.font-mono.text-xs');
  await expect(eventsList).toBeVisible({ timeout: 5_000 });
  // Should have at least one event item (op_started or op_completed).
  const items = eventsList.locator('li');
  await expect(items.first()).toBeVisible({ timeout: 5_000 });
  const count = await items.count();
  expect(count).toBeGreaterThan(0);

  // And the placeholder must NOT be showing.
  const placeholder = page.getByText('Waiting for events…');
  await expect(placeholder).toHaveCount(0);

  // Sanity: the status badge should also have updated to 'completed'
  // (the acquire.upload of a tiny mp4 takes <100ms; by 5s it's done
  // and the page has refresh()d the job row).
  await expect(page.getByText('completed', { exact: true })).toBeVisible({
    timeout: 5_000,
  });
});
