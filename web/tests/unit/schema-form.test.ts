import { describe, expect, it } from 'vitest';
import { initialParams } from '$lib/components/forms/schema';
import type { ParamsSchema } from '$lib/components/forms/schema';

// initialParams is the deterministic entry point — the form renderer
// itself is exercised by Playwright e2e (commits 41+, no DOM mocking
// here). These tests pin the shape contract every shipped op schema
// relies on.

describe('SchemaForm.initialParams', () => {
  it('uses defaults when present', () => {
    const schema: ParamsSchema = {
      properties: {
        model: { type: 'string', default: 'whisper-v3' },
        temperature: { type: 'number', default: 0.2 },
        word_timestamps: { type: 'boolean', default: true },
      },
    };
    expect(initialParams(schema)).toEqual({
      model: 'whisper-v3',
      temperature: 0.2,
      word_timestamps: true,
    });
  });

  it('falls back to null for nullable fields without a default', () => {
    const schema: ParamsSchema = {
      properties: {
        focus: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      },
    };
    expect(initialParams(schema)).toEqual({ focus: null });
  });

  it('falls back to typed zero/empty for non-nullable fields without a default', () => {
    const schema: ParamsSchema = {
      properties: {
        name: { type: 'string' },
        n: { type: 'integer' },
        on: { type: 'boolean' },
      },
    };
    expect(initialParams(schema)).toEqual({ name: '', n: 0, on: false });
  });

  it('hides readOnly fields (validator overwrites them anyway)', () => {
    // Mirrors IdentifyParams.speaker_db_sha contract.
    const schema: ParamsSchema = {
      properties: {
        speaker_db: { type: 'string', format: 'path' },
        speaker_db_sha: { type: 'string', readOnly: true, default: 'auto' },
      },
    };
    const init = initialParams(schema);
    expect(init).not.toHaveProperty('speaker_db_sha');
    expect(init).toHaveProperty('speaker_db');
  });

  it('returns an empty object when there are no properties', () => {
    expect(initialParams({})).toEqual({});
    expect(initialParams({ properties: {} })).toEqual({});
  });
});
