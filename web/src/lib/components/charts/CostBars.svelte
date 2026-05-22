<script lang="ts">
  import { scaleLinear } from 'd3-scale';

  type Row = {
    key: string;
    count: number;
    total_cents: number;
  };

  type Props = {
    rows: readonly Row[];
    /** Optional label override for the key column (e.g., "op", "backend"). */
    keyLabel?: string;
  };

  let { rows, keyLabel = 'key' }: Props = $props();

  // Bars scale to the widest row so the leader fills the track and the
  // others render proportional widths. Empty / zero-cost rows still
  // show a thin nub so they're visible.
  const ZERO_NUB_PCT = 0.5;
  let widths = $derived.by(() => {
    if (rows.length === 0) return new Map<string, number>();
    const max = Math.max(...rows.map((r) => r.total_cents), 0);
    if (max <= 0) {
      // All zero — every bar gets a tiny nub so the rows are visible
      // (this happens when the ledger only has local-cost ops).
      return new Map(rows.map((r) => [r.key, ZERO_NUB_PCT] as const));
    }
    const scale = scaleLinear().domain([0, max]).range([ZERO_NUB_PCT, 100]);
    return new Map(rows.map((r) => [r.key, scale(r.total_cents)] as const));
  });
</script>

{#if rows.length === 0}
  <p class="text-sm italic" style="color: var(--text-muted);">
    No cost-log rows in the selected window.
  </p>
{:else}
  <table class="w-full text-sm">
    <thead>
      <tr
        style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;"
      >
        <th class="text-left px-3 py-2 font-semibold w-1/4">{keyLabel}</th>
        <th class="text-right px-3 py-2 font-semibold w-16">runs</th>
        <th class="text-left px-3 py-2 font-semibold">spend</th>
        <th class="text-right px-3 py-2 font-semibold w-24">USD</th>
      </tr>
    </thead>
    <tbody>
      {#each rows as r (r.key)}
        <tr style="border-bottom: 1px solid var(--border-soft);">
          <td class="px-3 py-2 font-mono text-xs" style="color: var(--text-primary);">{r.key}</td>
          <td class="px-3 py-2 text-right font-mono text-xs" style="color: var(--text-secondary);">{r.count}</td>
          <td class="px-3 py-2">
            <div
              class="h-2 rounded"
              style="background: var(--border-soft); position: relative; overflow: hidden;"
            >
              <div
                class="h-full rounded"
                style="background: var(--accent-green); width: {widths.get(r.key) ?? 0}%;"
                aria-label="{r.key} spend bar"
              ></div>
            </div>
          </td>
          <td class="px-3 py-2 text-right font-mono text-xs" style="color: var(--text-secondary);">
            {(r.total_cents / 100).toFixed(4)}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}
