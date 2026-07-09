/**
 * Phase 8 — Profiles transparency + pre-run preflight regression spec.
 *
 * Drives a live `med web start`. Covers:
 *   1. Profiles list cards render model/provider digest badges.
 *   2. POST /pipelines/preview surfaces `fps × duration > max_frames`
 *      BEFORE running — the headline "errors appear before execution" fix.
 *
 * Run against a live server (token via env), same pattern as
 * settings_and_b005.spec.ts:
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

test('Profiles cards render model/provider digest badges', async ({ page }) => {
  await page.goto(`${baseURL}/ui/profiles`);
  // At least one bundled profile card shows a provider digest badge.
  await expect(page.locator('[data-testid="card-digest"]').first()).toBeVisible({
    timeout: 8_000,
  });
});

test('Pre-run preflight surfaces fps × duration > max_frames before running', async ({
  page,
}) => {
  // Seed a Video artifact.
  const fs = await import('fs/promises');
  const path = await import('path');
  const fixture = path.resolve(process.cwd(), '..', 'tests', 'fixtures', 'sample.mp4');
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
      file: { name: 'sample.mp4', mimeType: 'video/mp4', buffer: videoBuffer },
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
    await new Promise((res) => setTimeout(res, 250));
  }
  expect(artifactId).toBeTruthy();

  // Preflight a comprehend pipeline with fps=8 + max_frames=1 — guaranteed
  // over-budget for any real video. The error must surface in the preview,
  // NOT after a run.
  const yaml = [
    'name: preflight-test',
    'kind: pipeline',
    'inputs:',
    '  - { name: source, kind: video }',
    'graph:',
    '  - id: result',
    '    op: video.comprehend',
    '    inputs: { in: source }',
    '    params: { fps: 8.0, max_frames: 1 }',
    'outputs: [result]',
  ].join('\n');

  const preview = await page.request.post(`${baseURL}/pipelines/preview`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { pipeline_yaml: yaml, sources: [{ name: 'source', artifact_id: artifactId }] },
  });
  expect(preview.ok()).toBeTruthy();
  const body = await preview.json();
  expect(body.ok).toBe(true);
  const node = body.nodes.find((n: { id: string }) => n.id === 'result');
  expect(node.feasibility_error).toBeTruthy();
  expect(node.feasibility_error).toContain('max_frames');
});
