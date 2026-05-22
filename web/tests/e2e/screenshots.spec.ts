import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

/**
 * Phase 6 commit 50 — bundled Web UI screenshots.
 *
 * NOT part of the CI gate. Driven by `scripts/gen_ui_screenshots.sh`,
 * which boots an isolated `med web start` instance on :8765 with a
 * synthetic-fixture namespace, then runs this spec to capture each
 * panel.
 *
 * Environment contract:
 *
 *   MEDIA_ENGINE_WEB_E2E_BASE_URL   http://127.0.0.1:8765
 *   MEDIA_ENGINE_WEB_E2E_TOKEN      <bearer token, set in localStorage>
 *   MEDIA_ENGINE_WEB_E2E_OUT_DIR    docs/web_ui/   (PNGs land here)
 *
 * The /ui mount has a SPA-fallback handler (app.py commit 50) that
 * serves index.html for any /ui/<path> that doesn't match a real file,
 * so direct page.goto to deep routes works.
 */

const baseURL = process.env.MEDIA_ENGINE_WEB_E2E_BASE_URL ?? 'http://127.0.0.1:8765';
const token = process.env.MEDIA_ENGINE_WEB_E2E_TOKEN ?? '';
const outDir =
  process.env.MEDIA_ENGINE_WEB_E2E_OUT_DIR ??
  join(process.cwd(), '..', 'docs', 'web_ui');

mkdirSync(outDir, { recursive: true });

test.use({ viewport: { width: 1440, height: 900 } });

test.beforeEach(async ({ context }) => {
  // Surface browser-side errors to the worker log so SPA hydration
  // failures (CSP, module load, etc.) are visible without diving
  // into Playwright's HTML report.
  context.on('weberror', (err) => {
    // eslint-disable-next-line no-console
    console.log(`[browser-pageerror] ${err.error().message}`);
  });
  // Pre-seed the token for every page in the context so /ui/setup is
  // bypassed. Key shape mirrors web/src/lib/stores/token.ts.
  await context.addInitScript((bearer: string) => {
    if (bearer) {
      window.localStorage.setItem('media_engine:bearer', bearer);
    }
  }, token);
});

async function shoot(page: Page, name: string): Promise<void> {
  // Soft settle — let cost previews, SSE handshakes, and dagre layouts
  // converge before the capture frame. Don't wait for networkidle on
  // panels that open long-lived SSE streams (Jobs, Job detail) — those
  // never settle to idle.
  await page.waitForTimeout(800);
  await page.screenshot({
    path: join(outDir, `${name}.png`),
    fullPage: false,
  });
}

test('ingest panel', async ({ page }) => {
  await page.goto(`${baseURL}/ui/ingest`);
  await expect(page.getByRole('heading', { name: 'Ingest', exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await shoot(page, 'ingest');
});

test('run panel', async ({ page }) => {
  await page.goto(`${baseURL}/ui/run`);
  await expect(page.getByRole('heading', { name: /^Run an op$/i })).toBeVisible({
    timeout: 15_000,
  });
  // Pick an op so the schema form renders alongside the picker.
  const opCandidate = page.getByRole('button', { name: /speakers\.identify/i }).first();
  if (await opCandidate.count()) {
    await opCandidate.click().catch(() => undefined);
  }
  await page.waitForTimeout(500);
  await shoot(page, 'run');
});

test('jobs dashboard', async ({ page }) => {
  await page.goto(`${baseURL}/ui/jobs`);
  await expect(page.getByRole('heading', { name: 'Jobs', exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await shoot(page, 'jobs');
});

test('catalog detail — transcript preview', async ({ page }) => {
  // The seed script writes a Transcript at id = "b" * 64.
  const transcriptId = 'b'.repeat(64);
  await page.goto(`${baseURL}/ui/catalog/${transcriptId}`);
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(800);
  await shoot(page, 'catalog-detail');
});

test('lineage graph', async ({ page }) => {
  // SessionAnalysis at id = "d" * 64; lineage tab fans up through
  // Transcript + Video. The detail page's active tab is internal
  // state (not URL-driven); click the button to switch.
  const analysisId = 'd'.repeat(64);
  await page.goto(`${baseURL}/ui/catalog/${analysisId}`);
  await page.waitForLoadState('domcontentloaded');
  await page
    .waitForResponse((r) => r.url().includes(`/artifacts/${analysisId}/lineage`))
    .catch(() => undefined);
  // Use page.evaluate to dispatch the click — Playwright's locator
  // click is firing but the {#if} block doesn't seem to react in
  // Svelte 5 strict-mode without a settled microtask flush; the
  // direct dispatch + a small delay gives the reactive update time
  // to propagate.
  await page.evaluate(() => {
    const buttons = Array.from(document.querySelectorAll('button'));
    const lineage = buttons.find((b) => b.textContent?.trim() === 'Lineage');
    lineage?.click();
  });
  // dagre layout + Svelte Flow first paint needs a couple of beats.
  await page.waitForTimeout(2_500);
  await shoot(page, 'lineage');
});

test('profile workspace', async ({ page }) => {
  // The bundled `analysis-full` profile is always present.
  await page.goto(`${baseURL}/ui/profiles/analysis-full`);
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(900);
  await shoot(page, 'profile-workspace');
});
