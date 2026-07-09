import { describe, expect, it, vi } from 'vitest';
import {
  formatBytes,
  getConfigFiles,
  getDoctor,
  isRevoked,
  listSecrets,
  putConfigFiles,
  putSecrets,
  type TokenInfo,
} from '$lib/api/settings';
import { api } from '$lib/api/client';

describe('formatBytes', () => {
  it('renders zero / negative / NaN as "0 B"', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(-5)).toBe('0 B');
    expect(formatBytes(Number.NaN)).toBe('0 B');
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe('0 B');
  });

  it('renders sub-KB as integer bytes with B unit', () => {
    expect(formatBytes(1)).toBe('1 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1023)).toBe('1023 B');
  });

  it('scales on powers of 1024 with one decimal where useful', () => {
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB');
    expect(formatBytes(1024 * 1024 * 1024)).toBe('1.0 GB');
    expect(formatBytes(1024 ** 4)).toBe('1.0 TB');
  });

  it('drops the decimal at >= 10 of a unit', () => {
    expect(formatBytes(10 * 1024)).toBe('10 KB');
    expect(formatBytes(1024 * 1024 * 1024 * 1024 * 5)).toBe('5.0 TB');
  });
});

describe('isRevoked', () => {
  const baseline: TokenInfo = {
    id: 't1',
    label: 'laptop',
    namespace: 'default',
    created_at: '2026-05-20T00:00:00Z',
    revoked_at: null,
  };

  it('returns false when revoked_at is null', () => {
    expect(isRevoked(baseline)).toBe(false);
  });

  it('returns true when revoked_at carries an ISO string', () => {
    expect(isRevoked({ ...baseline, revoked_at: '2026-05-22T00:00:00Z' })).toBe(true);
  });

  it('matches the server contract — `revoked_at != None` is the truth', () => {
    // Even an empty string would be truthy in JS; we use strict null
    // so the server's "null" sentinel is the *only* not-revoked state.
    expect(isRevoked({ ...baseline, revoked_at: '' })).toBe(true);
  });
});

describe('Settings REST client (Doctor / Secrets / Config files)', () => {
  it('getDoctor() encodes the op filter into ?op=', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValueOnce({ summary: {}, ops: [] });
    await getDoctor('intelligence.');
    expect(spy).toHaveBeenCalledWith('/settings/doctor?op=intelligence.');
    spy.mockRestore();
  });

  it('getDoctor() omits ?op= when no filter is passed', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValueOnce({ summary: {}, ops: [] });
    await getDoctor();
    expect(spy).toHaveBeenCalledWith('/settings/doctor');
    spy.mockRestore();
  });

  it('listSecrets() targets /settings/secrets', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValueOnce({ items: [], file_path: '' });
    await listSecrets();
    expect(spy).toHaveBeenCalledWith('/settings/secrets');
    spy.mockRestore();
  });

  it('putSecrets() wraps updates under the `updates` key', async () => {
    const spy = vi
      .spyOn(api, 'put')
      .mockResolvedValueOnce({ items: [], file_path: '', written: [] });
    await putSecrets({ GEMINI_API_KEY: 'abc', HF_TOKEN: null });
    expect(spy).toHaveBeenCalledWith('/settings/secrets', {
      updates: { GEMINI_API_KEY: 'abc', HF_TOKEN: null },
    });
    spy.mockRestore();
  });

  it('getConfigFiles() targets /settings/config-files', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValueOnce({
      config_toml: { path: '', exists: false, content: '', is_masked: false },
      resources_yaml: { path: '', exists: false, content: '', is_masked: false },
      secrets_env: { path: '', exists: false, content: '', is_masked: true },
    });
    await getConfigFiles();
    expect(spy).toHaveBeenCalledWith('/settings/config-files');
    spy.mockRestore();
  });

  it('putConfigFiles() PUTs only the provided file(s) — no secrets field', async () => {
    const spy = vi.spyOn(api, 'put').mockResolvedValueOnce({
      config_toml: { path: '', exists: true, content: 'namespace = "x"', is_masked: false },
      resources_yaml: { path: '', exists: false, content: '', is_masked: false },
      secrets_env: { path: '', exists: false, content: '', is_masked: true },
    });
    await putConfigFiles({ config_toml: 'namespace = "x"' });
    // Exact-match deep equality ⇒ the body is *only* { config_toml }, proving
    // there's no secrets channel smuggled through this endpoint.
    expect(spy).toHaveBeenCalledWith('/settings/config-files', {
      config_toml: 'namespace = "x"',
    });
    spy.mockRestore();
  });
});
