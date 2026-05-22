import { describe, expect, it } from 'vitest';
import { ARTIFACT_KINDS, artifactFileUrl } from '$lib/api/artifacts';

describe('Artifact REST helpers', () => {
  it('lists every engine kind so the catalog filter can iterate', () => {
    // Mirror media_engine/artifacts/base.py:Kind — drift here means the
    // catalog browser will silently miss a kind.
    expect(ARTIFACT_KINDS).toEqual([
      'video',
      'audio',
      'image',
      'frameset',
      'transcript',
      'diarization',
      'ocrtext',
      'chunks',
      'embedding',
      'analysis',
      'session_analysis',
      'markdown',
      'document',
      'webpage',
    ]);
  });

  it('builds /file URLs anchored at /artifacts/{id}/file', () => {
    // No token in the test store, so the URL is the bare /file path.
    expect(artifactFileUrl('abc123')).toBe('/artifacts/abc123/file');
  });

  it('encodes id in the path segment', () => {
    expect(artifactFileUrl('a/b')).toBe('/artifacts/a%2Fb/file');
  });
});
