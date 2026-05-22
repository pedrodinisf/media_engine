import { describe, expect, it } from 'vitest';
import { tokens } from '$lib/theme/tokens';

// Smoke test for the design-token mirror. The full visual story is
// covered by Playwright + screenshots; this just locks the shape so a
// careless rename of the .ts mirror breaks tests before it breaks
// downstream component imports.
describe('Clean-NASA tokens', () => {
  it('exposes the canonical light palette', () => {
    expect(tokens.bg.page).toBe('#F5F4EE');
    expect(tokens.bg.header).toBe('#2A3328');
    expect(tokens.accent.green).toBe('#059669');
    expect(tokens.text.primary).toBe('#2D2D2D');
  });

  it('exposes font stacks for sans + mono', () => {
    expect(tokens.font.sans).toContain('system-ui');
    expect(tokens.font.mono).toContain('Menlo');
  });
});
