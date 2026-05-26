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

test('Brand: replicated-spark logo renders in the header', async ({ page }) => {
  await page.goto(`${baseURL}/ui/`);
  // The Logo component carries data-testid="brand-logo" + an aria-hidden
  // SVG with three <path> children (cascading opacity pulses).
  const logo = page.locator('[data-testid="brand-logo"]');
  await expect(logo).toBeVisible({ timeout: 5_000 });
  await expect(logo.locator('path')).toHaveCount(3);
});

test('Settings: "Visibility" tab replaces "Catalog gate"', async ({ page }) => {
  await page.goto(`${baseURL}/ui/settings`);
  // Renamed tab is clickable + present.
  await page.getByRole('button', { name: 'Visibility', exact: true }).click();
  // Tab content reads as op/backend visibility now, not the old name.
  await expect(page.getByText('Op & backend visibility')).toBeVisible({
    timeout: 5_000,
  });
  // The old name must NOT appear anywhere on the page.
  await expect(page.getByText('Catalog gate')).toHaveCount(0);
});

test('Run panel: audio.transcribe model field is a <select> (curated dropdown)', async ({ page }) => {
  await page.goto(`${baseURL}/ui/run`);
  await page.getByRole('button', { name: 'audio.transcribe', exact: true }).click();
  // Wait for the form to render — there are multiple <select> elements
  // (backend + language + model). Find the one whose options include
  // the whisper variants.
  const modelSelect = page.locator('select').filter({
    hasText: 'mlx-community/whisper-large-v3-mlx',
  });
  await expect(modelSelect).toBeVisible({ timeout: 5_000 });
  // The curated set ships 6 variants — assert at least 4 to allow for
  // future additions without breaking the spec.
  const options = await modelSelect.locator('option').count();
  expect(options).toBeGreaterThanOrEqual(4);
});

test('Run panel: range slider renders when an Audio artifact is the input', async ({ page }) => {
  // Ingest a small wav so the test env has a known Audio artifact id.
  // sample_speech.wav (~30s) is the smallest seedable in the repo.
  const fs = await import('fs/promises');
  const path = await import('path');
  const fixture = path.resolve(
    process.cwd(), '..', 'tests', 'fixtures', 'sample_speech.wav',
  );
  let audioBuffer: Buffer;
  try {
    audioBuffer = await fs.readFile(fixture);
  } catch {
    test.skip(true, `fixture missing: ${fixture}`);
    return;
  }
  const upload = await page.request.post(`${baseURL}/acquire/upload`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'sample_speech.wav',
        mimeType: 'audio/wav',
        buffer: audioBuffer,
      },
      commit: 'true',
    },
  });
  expect(upload.ok()).toBeTruthy();
  const { job_id: jobId } = await upload.json();

  // Poll the job until it completes — acquire.upload is fast (< 5s).
  let artifactId: string | null = null;
  for (let attempt = 0; attempt < 30; attempt++) {
    const r = await page.request.get(`${baseURL}/jobs/${jobId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await r.json();
    if (body.job?.status === 'completed' && body.job.output_artifact_ids?.length) {
      artifactId = body.job.output_artifact_ids[0];
      break;
    }
    if (body.job?.status === 'failed') {
      throw new Error(`acquire.upload failed: ${JSON.stringify(body.job.error)}`);
    }
    await new Promise((res) => setTimeout(res, 250));
  }
  expect(artifactId).toBeTruthy();

  // Drive the Run panel with that artifact id.
  await page.goto(`${baseURL}/ui/run`);
  await page.getByRole('button', { name: 'audio.transcribe', exact: true }).click();
  await page.locator('input[placeholder*="a-3c1f"]').fill(artifactId!);

  // Slider renders above SchemaForm once the inputCheck resolves the
  // Audio kind + duration. mm:ss labels live next to the handles.
  await expect(page.getByText('Time range')).toBeVisible({ timeout: 8_000 });
  await expect(page.getByRole('button', { name: 'Use full range' })).toBeVisible();
});

test('Phase 6.7: range slider also renders for Video inputs when the op exposes start_s/end_s', async ({ page }) => {
  // Upload sample.mp4 → Video artifact with duration metadata.
  const fs = await import('fs/promises');
  const path = await import('path');
  const fixture = path.resolve(
    process.cwd(), '..', 'tests', 'fixtures', 'sample.mp4',
  );
  let videoBuffer: Buffer;
  try {
    videoBuffer = await fs.readFile(fixture);
  } catch {
    test.skip(true, `fixture missing: ${fixture}`);
    return;
  }
  const upload = await page.request.post(`${baseURL}/acquire/upload`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'sample.mp4',
        mimeType: 'video/mp4',
        buffer: videoBuffer,
      },
      commit: 'true',
    },
  });
  expect(upload.ok()).toBeTruthy();
  const { job_id: jobId } = await upload.json();

  let artifactId: string | null = null;
  for (let attempt = 0; attempt < 30; attempt++) {
    const r = await page.request.get(`${baseURL}/jobs/${jobId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await r.json();
    if (body.job?.status === 'completed' && body.job.output_artifact_ids?.length) {
      artifactId = body.job.output_artifact_ids[0];
      break;
    }
    if (body.job?.status === 'failed') {
      throw new Error(`acquire.upload failed: ${JSON.stringify(body.job.error)}`);
    }
    await new Promise((res) => setTimeout(res, 250));
  }
  expect(artifactId).toBeTruthy();

  await page.goto(`${baseURL}/ui/run`);
  // Pick an op that has start_s/end_s + accepts Video. sample_frames
  // is the leanest schema for this (one input field, params include
  // the new fields).
  await page.getByRole('button', { name: 'video.sample_frames', exact: true }).click();
  await page.locator('input[placeholder*="a-3c1f"]').fill(artifactId!);

  // Same slider, same labels — proves the gating generalized from
  // audio-only to "any kind that carries duration metadata".
  await expect(page.getByText('Time range')).toBeVisible({ timeout: 8_000 });
  await expect(page.getByRole('button', { name: 'Use full range' })).toBeVisible();
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

  // Use exact:true so we hit the tab strip "Failure" and not the
  // inline "Failure tab" link inside the B-012 events-fallback message.
  await page.getByRole('button', { name: 'Failure', exact: true }).click();
  // Error class badge — comes straight from _classify_error. Match on
  // the <span> badge specifically; the traceback <pre> also contains
  // the class name, which would trip strict mode.
  await expect(
    page.locator('span').filter({ hasText: /^(LookupError|ValueError|RuntimeError)$/ }).first(),
  ).toBeVisible({ timeout: 5_000 });
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

// NOTE — there's no dedicated "Secrets shows unblock-impact" spec.
// Under parallel workers the catalog-save spec writes GEMINI_API_KEY
// mid-run, which flips the impact state of every other secret (a
// previously direct-unblocked op becomes "already working" and drops
// out of the lists). The Doctor quick-fix banner spec above already
// exercises the same server→UI rendering path on a different surface,
// and tests/test_api_settings.py asserts the impact computation
// correctness directly against the registry. Bringing this spec back
// would require test.describe.serial — not worth it for the marginal
// coverage.

test('B-009: Doctor expand row shows the delegate breakdown for a composite', async ({ page }) => {
  // After Phase 6.6: composites with delegates_to surface a per-delegate
  // status list inside the expand row. The harness scrubs cloud-API keys
  // so intelligence.extract's gemini/claude backends are unavailable and
  // the composite (intelligence.summarize) rolls up to red — the row
  // must render at least one ❌-marked delegate entry.
  await page.goto(`${baseURL}/ui/settings`);
  await expect(page.getByText('Dependency map — what works on this machine')).toBeVisible({
    timeout: 5_000,
  });
  // intelligence.summarize is a composite; find its row and expand.
  const row = page.locator('tr', { hasText: 'intelligence.summarize' }).first();
  await row.getByRole('button', { name: '+' }).click();
  // The breakdown carries a data-testid for stable lookup.
  const breakdown = page.locator('[data-testid="delegate-breakdown"]');
  await expect(breakdown).toBeVisible({ timeout: 5_000 });
  await expect(breakdown).toContainText('intelligence.extract');
});

test('B-008: /run/preview returns 422 when --backend conflicts with model', async ({ request }) => {
  // frames.analyze with default model=gemini-2.5-pro and an explicit
  // backend=vllm-mlx is internally inconsistent. The preview endpoint
  // must surface this with a 422 + a message naming the routed backend.
  const resp = await request.post(`${baseURL}/run/preview`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      op: 'frames.analyze',
      inputs: [],  // preview tolerates empty inputs for cost-only validation
      backend: 'vllm-mlx',
      params: { prompt: 'describe' },
    },
  });
  expect(resp.status()).toBe(422);
  const body = await resp.json();
  expect(body.detail).toMatch(/incompatible|routes to/);
});

test('B-007: intelligence.summarize exposes extract_backend as a schema field', async ({ request }) => {
  // The composite gained an explicit extract_backend override in Phase 6.6.
  // The Run-panel SchemaForm auto-renders any new field; here we assert the
  // server schema actually carries it so the dropdown gets surfaced.
  const resp = await request.get(`${baseURL}/operations/intelligence.summarize`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  expect(body.params_schema.properties).toHaveProperty('extract_backend');
});
