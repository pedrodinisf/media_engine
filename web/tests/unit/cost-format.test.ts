import { describe, expect, it } from 'vitest';
import { COST_GROUP_BY, monthlyBurnProjection } from '$lib/api/cost';
import { SEARCH_MODES } from '$lib/api/search';

describe('cost helpers', () => {
  it('exposes every group-by axis the route accepts', () => {
    // Mirror media_engine/api/cost_routes.py group_by Literal — drift
    // means the UI offers a key the server will 422 on.
    expect(COST_GROUP_BY).toEqual(['op', 'backend', 'namespace']);
  });

  it('projects monthly burn linearly from a window total', () => {
    // Window: 1 day = 86_400_000 ms; ~$1 → projects to ~$30/month.
    const start = '2026-05-01T00:00:00Z';
    const end = '2026-05-02T00:00:00Z';
    const projection = monthlyBurnProjection(100, start, end);
    expect(projection).not.toBeNull();
    expect(projection).toBeCloseTo(30, 0);
  });

  it('returns 0 for zero spend', () => {
    expect(monthlyBurnProjection(0, '2026-05-01T00:00:00Z', '2026-05-02T00:00:00Z')).toBe(0);
  });

  it('returns null for sub-minute windows (no signal)', () => {
    const start = '2026-05-01T00:00:00Z';
    const end = '2026-05-01T00:00:30Z';
    expect(monthlyBurnProjection(50, start, end)).toBeNull();
  });
});

describe('search helpers', () => {
  it('lists the three engine search modes the FE supports', () => {
    expect(SEARCH_MODES).toEqual(['fulltext', 'semantic', 'hybrid']);
  });
});
