/*
 * Helpers for FloatInput.svelte. Extracted so they're unit-testable
 * without spinning up Svelte runtime (the svelte module boundary makes
 * importing component-local fns into vitest awkward).
 */

/**
 * Strings the user can be mid-typing that don't yet round-trip cleanly
 * through Number() — trailing dot ("0."), lone sign ("-"), bare dot ("."),
 * signed bare dot ("-."). These should NOT trigger a re-sync from the
 * parent's controlled value, or the input would lose the in-progress dot.
 */
export function isIntermediate(s: string): boolean {
  return /^-?$|\.$|^-?\.$/.test(s.replace(',', '.'));
}

/**
 * Parse a user-entered string into a number, accepting either a period or
 * a comma as the decimal separator (European keyboards). Returns null for
 * empty or non-numeric input.
 */
export function parseFloatInput(s: string): number | null {
  const raw = s.replace(',', '.').trim();
  if (raw === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}
