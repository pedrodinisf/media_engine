import { describe, expect, it } from 'vitest';
import {
  PROFILE_NAME_RE,
  forkPayload,
  isValidProfileName,
} from '$lib/profile/api';
import type {
  PipelineProfile,
  PromptProfile,
} from '$lib/profile/types';

describe('profile name validator', () => {
  it('accepts conventional kebab-case names', () => {
    expect(isValidProfileName('analysis-full')).toBe(true);
    expect(isValidProfileName('my-pipeline')).toBe(true);
    expect(isValidProfileName('a')).toBe(true);
    expect(isValidProfileName('123-test')).toBe(true);
    expect(isValidProfileName('foo_bar-baz_42')).toBe(true);
  });

  it('rejects path traversal attempts', () => {
    expect(isValidProfileName('../etc/passwd')).toBe(false);
    expect(isValidProfileName('..')).toBe(false);
    expect(isValidProfileName('foo/bar')).toBe(false);
    expect(isValidProfileName('foo\\bar')).toBe(false);
  });

  it('rejects uppercase + whitespace + leading dash', () => {
    expect(isValidProfileName('MyPipeline')).toBe(false);
    expect(isValidProfileName('my pipeline')).toBe(false);
    expect(isValidProfileName('-leading-dash')).toBe(false);
    expect(isValidProfileName('_leading-underscore')).toBe(false);
  });

  it('rejects empty + too-long names', () => {
    expect(isValidProfileName('')).toBe(false);
    expect(isValidProfileName('a'.repeat(65))).toBe(false);
  });

  it('the regex source matches the Python server-side regex', () => {
    // Mirror media_engine/api/routes.py:_PROFILE_NAME_RE — drift
    // here means the UI accepts names the server will 400 on.
    expect(PROFILE_NAME_RE.source).toBe('^[a-z0-9][a-z0-9_-]{0,63}$');
  });
});

describe('ProfileSummary shape', () => {
  it('mirrors the server source field literal', () => {
    // Mirror media_engine/api/routes.py:ProfileSummary.source ∈
    // Literal["bundled", "user"] — drift means the index card UI
    // would silently miscategorise.
    const sample: import('$lib/profile/types').ProfileSummary = {
      name: 'x',
      kind: 'pipeline',
      description: '',
      path: '/some/path',
      source: 'user',
    };
    expect(sample.source).toBe('user');
    // Type-level: assigning anything outside {bundled, user} would
    // fail svelte-check; this is the runtime smoke test.
    const sources: Array<'bundled' | 'user'> = ['bundled', 'user'];
    expect(sources).toContain(sample.source);
  });
});

describe('forkPayload', () => {
  const pipeline: PipelineProfile = {
    profile_schema_version: '1.0',
    name: 'analysis-full',
    kind: 'pipeline',
    description: 'sample',
    inputs: [{ name: 'source', kind: 'video' }],
    graph: [
      {
        id: 'audio',
        op: 'video.extract_audio',
        inputs: { in: 'source' },
        params: {},
        backend: null,
        depends_on: [],
      },
    ],
    outputs: ['audio'],
  };

  const prompt: PromptProfile = {
    profile_schema_version: '1.0',
    name: 'cooking-recipes',
    kind: 'prompt',
    description: 'recipe lens',
    default_op: 'video.multimodal',
    default_backend: 'gemini',
    schema_path: null,
    body: '# Recipe lens\n\nProduce a recipe card.',
  };

  it('preserves the pipeline body, only renames', () => {
    const out = forkPayload(pipeline, 'my-fork');
    expect(out.kind).toBe('pipeline');
    expect(out.name).toBe('my-fork');
    if (out.kind === 'pipeline') {
      expect(out.graph).toEqual(pipeline.graph);
      expect(out.inputs).toEqual(pipeline.inputs);
      expect(out.outputs).toEqual(pipeline.outputs);
    }
  });

  it('preserves the prompt body, only renames', () => {
    const out = forkPayload(prompt, 'my-cooking-fork');
    expect(out.kind).toBe('prompt');
    expect(out.name).toBe('my-cooking-fork');
    if (out.kind === 'prompt') {
      expect(out.body).toBe(prompt.body);
      expect(out.default_op).toBe(prompt.default_op);
      expect(out.default_backend).toBe(prompt.default_backend);
    }
  });

  it('throws on path-traversal in the new name', () => {
    expect(() => forkPayload(pipeline, '../escape')).toThrow(/invalid profile name/);
    expect(() => forkPayload(pipeline, 'foo/bar')).toThrow(/invalid profile name/);
  });

  it('throws on uppercase / spaces', () => {
    expect(() => forkPayload(prompt, 'My Fork')).toThrow();
    expect(() => forkPayload(prompt, 'MyFork')).toThrow();
  });

  it('does not mutate the source object', () => {
    const before = JSON.stringify(pipeline);
    forkPayload(pipeline, 'mutation-check');
    expect(JSON.stringify(pipeline)).toBe(before);
  });
});
