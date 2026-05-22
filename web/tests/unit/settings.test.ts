import { describe, expect, it } from 'vitest';
import { formatBytes, isRevoked, type TokenInfo } from '$lib/api/settings';

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
