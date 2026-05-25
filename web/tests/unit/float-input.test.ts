import { describe, expect, it } from 'vitest';
import { isIntermediate, parseFloatInput } from '$lib/components/forms/float_input';

// Regression coverage for B-004 (locale leak: Temperature renders as 0,2 on
// pt-PT). The component uses these helpers to keep a local text buffer in
// canonical period-decimal form; what the DOM displays comes from String()
// of the parent's number, never the OS locale's decimal separator.

describe('parseFloatInput', () => {
  it('parses period-decimal strings', () => {
    expect(parseFloatInput('0.2')).toBe(0.2);
    expect(parseFloatInput('-1.5')).toBe(-1.5);
    expect(parseFloatInput('42')).toBe(42);
  });

  it('accepts comma as decimal separator (European keyboard)', () => {
    expect(parseFloatInput('0,2')).toBe(0.2);
    expect(parseFloatInput('-1,5')).toBe(-1.5);
  });

  it('returns null for empty / non-numeric input', () => {
    expect(parseFloatInput('')).toBeNull();
    expect(parseFloatInput('   ')).toBeNull();
    expect(parseFloatInput('abc')).toBeNull();
    expect(parseFloatInput('.')).toBeNull();
    expect(parseFloatInput('-')).toBeNull();
  });
});

describe('isIntermediate', () => {
  it('flags in-progress strings that should defer parent sync', () => {
    expect(isIntermediate('')).toBe(true);
    expect(isIntermediate('-')).toBe(true);
    expect(isIntermediate('0.')).toBe(true);
    expect(isIntermediate('-0.')).toBe(true);
    expect(isIntermediate('.')).toBe(true);
    expect(isIntermediate('-.')).toBe(true);
    // Comma form too — pt-PT user typing "0," is mid-edit.
    expect(isIntermediate('0,')).toBe(true);
  });

  it('does not flag fully-parsed values', () => {
    expect(isIntermediate('0.2')).toBe(false);
    expect(isIntermediate('-1.5')).toBe(false);
    expect(isIntermediate('42')).toBe(false);
  });
});

describe('B-004 locale rendering', () => {
  it('String() of a number always uses period decimal', () => {
    // Sanity check: the fix relies on String(0.2) === "0.2" regardless of
    // navigator.language. JavaScript's Number.prototype.toString is
    // locale-independent (Intl.NumberFormat is the locale-aware path).
    expect(String(0.2)).toBe('0.2');
    expect(String(-1.5)).toBe('-1.5');
    expect(String(42)).toBe('42');
  });
});
