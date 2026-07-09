import { describe, expect, it } from 'vitest';
import { classifyModelProvider, isModelField } from '$lib/components/forms/schema';
import { loadDocument, mutateNodeParams, serializeDocument } from '$lib/profile/parse';
import { parse as yamlParse } from 'yaml';

describe('classifyModelProvider', () => {
  it('tags known cloud prefixes', () => {
    expect(classifyModelProvider('gemini-2.5-pro')).toBe('cloud');
    expect(classifyModelProvider('claude-opus-4-7')).toBe('cloud');
    expect(classifyModelProvider('gpt-4o')).toBe('cloud');
  });
  it('tags known local prefixes', () => {
    expect(classifyModelProvider('mlx-community/Qwen2-VL-2B-Instruct-4bit')).toBe('local');
    expect(classifyModelProvider('pyannote/speaker-diarization-3.1')).toBe('local');
    expect(classifyModelProvider('sentence-transformers/all-MiniLM-L6-v2')).toBe('local');
  });
  it('is unknown for unrecognized ids', () => {
    expect(classifyModelProvider('my-custom-model')).toBe('unknown');
  });
});

describe('isModelField', () => {
  it('matches `model` and `*_model`', () => {
    expect(isModelField('model')).toBe(true);
    expect(isModelField('vlm_model')).toBe(true);
    expect(isModelField('synth_model')).toBe(true);
  });
  it('does not match non-model fields', () => {
    expect(isModelField('style')).toBe(false);
    expect(isModelField('models_dir')).toBe(false);
    expect(isModelField('max_frames')).toBe(false);
  });
});

const PIPELINE = `name: t
kind: pipeline
graph:
  - id: a
    op: frames.analyze
    params:
      prompt: describe
`;

describe('mutateNodeParams', () => {
  it('writes a non-default value into the node params', () => {
    const doc = loadDocument(PIPELINE);
    mutateNodeParams(doc, 'a', { prompt: 'describe', model: 'gemini-2.5-pro' }, {
      prompt: '',
      model: 'gemini-2.5-pro',
    });
    // model equals its default → omitted; prompt differs from default '' → kept.
    const out = yamlParse(serializeDocument(doc));
    expect(out.graph[0].params.prompt).toBe('describe');
    expect(out.graph[0].params.model).toBeUndefined();
  });

  it('writes a value that differs from its default', () => {
    const doc = loadDocument(PIPELINE);
    mutateNodeParams(
      doc,
      'a',
      { prompt: 'describe', model: 'mlx-community/Qwen2-VL-2B-Instruct-4bit' },
      { prompt: '', model: 'gemini-2.5-pro' },
    );
    const out = yamlParse(serializeDocument(doc));
    expect(out.graph[0].params.model).toBe('mlx-community/Qwen2-VL-2B-Instruct-4bit');
  });

  it('drops the params map entirely when everything reverts to default', () => {
    const doc = loadDocument(PIPELINE);
    mutateNodeParams(doc, 'a', { prompt: '' }, { prompt: '' });
    const out = yamlParse(serializeDocument(doc));
    expect(out.graph[0].params).toBeUndefined();
  });

  it('returns false for an unknown node id', () => {
    const doc = loadDocument(PIPELINE);
    expect(mutateNodeParams(doc, 'nope', { x: 1 }, {})).toBe(false);
  });
});
