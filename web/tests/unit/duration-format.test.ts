import { describe, expect, it } from 'vitest';
import { formatDuration } from '$lib/format/duration';

describe('formatDuration', () => {
  it('renders sub-minute as mm:ss', () => {
    expect(formatDuration(0)).toBe('00:00');
    expect(formatDuration(5)).toBe('00:05');
    expect(formatDuration(59)).toBe('00:59');
  });

  it('renders minutes:seconds for under an hour', () => {
    expect(formatDuration(60)).toBe('01:00');
    expect(formatDuration(3599)).toBe('59:59');
    expect(formatDuration(125)).toBe('02:05');
  });

  it('switches to h:mm:ss at exactly one hour', () => {
    expect(formatDuration(3600)).toBe('1:00:00');
    expect(formatDuration(3661)).toBe('1:01:01');
    expect(formatDuration(7325)).toBe('2:02:05');
  });

  it('floors fractional seconds (no rounding tricks)', () => {
    expect(formatDuration(5.9)).toBe('00:05');
    expect(formatDuration(59.9)).toBe('00:59');
    expect(formatDuration(60.1)).toBe('01:00');
  });

  it('collapses garbage inputs to "00:00" instead of rendering NaN/Infinity', () => {
    expect(formatDuration(-1)).toBe('00:00');
    expect(formatDuration(Number.NaN)).toBe('00:00');
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe('00:00');
    expect(formatDuration(Number.NEGATIVE_INFINITY)).toBe('00:00');
  });
});
