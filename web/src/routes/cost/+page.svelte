<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { ApiError } from '$lib/api/client';
  import {
    COST_GROUP_BY,
    fetchCostLog,
    fetchCostSummary,
    isoToLocalInputValue,
    localInputValueToIso,
    monthlyBurnProjection,
    type CostGroupBy,
    type CostLogResponse,
    type CostSummaryResponse,
  } from '$lib/api/cost';
  import CostBars from '$lib/components/charts/CostBars.svelte';

  // Default window: last 30 days. The state holds UTC ISO strings so
  // the same canonical form goes to /cost/summary and /cost/log; the
  // datetime-local inputs use a local-time bridge via $derived setters.
  function isoMinusDays(days: number): string {
    const d = new Date();
    d.setDate(d.getDate() - days);
    return d.toISOString();
  }

  let groupBy = $state<CostGroupBy>('op');

  // Compute the initial UTC ISO + matching local-input strings at
  // module init so the two $state pairs start in sync; the dedicated
  // commit helpers below keep them in sync on user edits. Pre-computing
  // also silences svelte/state_referenced_locally — the local-string
  // states must not reactively depend on the ISO states (the inputs
  // are the source of truth post-init).
  const _initSinceIso = isoMinusDays(30);
  const _initUntilIso = new Date().toISOString();

  let sinceIso = $state(_initSinceIso);
  let untilIso = $state(_initUntilIso);
  let sinceLocal = $state(isoToLocalInputValue(_initSinceIso));
  let untilLocal = $state(isoToLocalInputValue(_initUntilIso));

  function commitSinceLocal(v: string): void {
    sinceLocal = v;
    const iso = localInputValueToIso(v);
    if (iso) sinceIso = iso;
  }
  function commitUntilLocal(v: string): void {
    untilLocal = v;
    const iso = localInputValueToIso(v);
    if (iso) untilIso = iso;
  }

  let summary = $state<CostSummaryResponse | null>(null);
  let summaryError = $state<string | null>(null);
  let summaryLoading = $state(false);

  let log = $state<CostLogResponse | null>(null);
  let logError = $state<string | null>(null);
  let logLoading = $state(false);
  let logOffset = $state(0);

  async function loadSummary(): Promise<void> {
    summaryLoading = true;
    summaryError = null;
    try {
      // Read inside untrack — the $effect that drives this only wants
      // to refire on groupBy changes; the date inputs are explicit
      // (Refresh button) so users can finish typing without firing N
      // requests mid-keystroke.
      const [g, s, u] = untrack(() => [groupBy, sinceIso, untilIso]);
      summary = await fetchCostSummary({ group_by: g, since: s, until: u });
    } catch (e) {
      summaryError = e instanceof ApiError ? e.detail : String(e);
      summary = null;
    } finally {
      summaryLoading = false;
    }
  }

  async function loadLog(reset: boolean): Promise<void> {
    logLoading = true;
    logError = null;
    if (reset) logOffset = 0;
    try {
      const [s, u, off] = untrack(() => [sinceIso, untilIso, logOffset]);
      const page = await fetchCostLog({
        since: s, until: u, limit: 50, offset: off,
      });
      if (reset || log === null) {
        log = page;
      } else {
        log = {
          ...page,
          items: [...log.items, ...page.items],
        };
      }
    } catch (e) {
      logError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      logLoading = false;
    }
  }

  async function loadMore(): Promise<void> {
    if (log?.next_offset === null || log === null) return;
    logOffset = log.next_offset;
    await loadLog(false);
  }

  // Single source of truth for re-fetching the rollup: any time the
  // user picks a different group-by axis. The first run (on mount)
  // also drives the initial fetch — no separate onMount call, so no
  // race between a stale `summary === null` gate and an in-flight
  // first load.
  $effect(() => {
    const _g = groupBy;
    void _g; // tracked dependency
    void loadSummary();
  });

  // The log doesn't auto-refetch on filter changes (the Refresh
  // button does); we just need an initial load.
  onMount(() => {
    void loadLog(true);
  });

  // Projection is anchored to the echoed summary window — NOT live
  // sinceIso/untilIso — so the displayed projection always matches
  // the displayed rollup, even mid-typing.
  let projection = $derived.by(() => {
    if (!summary) return null;
    const winStart = summary.since ?? sinceIso;
    const winEnd = summary.until ?? untilIso;
    return monthlyBurnProjection(summary.total_cents, winStart, winEnd);
  });
</script>

<svelte:head>
  <title>media_engine · Cost</title>
</svelte:head>

<header class="mb-5">
  <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Cost ledger</h1>
  <p class="text-sm" style="color: var(--text-secondary);">
    Actual spend recorded by <code class="font-mono text-xs">Engine.run</code>. Cache hits are free
    and not logged.
  </p>
</header>

<section
  class="p-4 rounded mb-4 grid grid-cols-12 gap-3 items-end"
  style="background: var(--bg-card); border: 1px solid var(--border-soft);"
>
  <label class="col-span-3">
    <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">
      Since <span style="color: var(--text-muted);">(local)</span>
    </span>
    <input
      type="datetime-local"
      value={sinceLocal}
      oninput={(e) => commitSinceLocal((e.currentTarget as HTMLInputElement).value)}
      class="w-full px-2 py-1.5 rounded text-xs font-mono"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
    />
  </label>
  <label class="col-span-3">
    <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">
      Until <span style="color: var(--text-muted);">(local)</span>
    </span>
    <input
      type="datetime-local"
      value={untilLocal}
      oninput={(e) => commitUntilLocal((e.currentTarget as HTMLInputElement).value)}
      class="w-full px-2 py-1.5 rounded text-xs font-mono"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
    />
  </label>
  <label class="col-span-3">
    <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">Group by</span>
    <select
      bind:value={groupBy}
      class="w-full px-2 py-1.5 rounded text-xs font-mono"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
    >
      {#each COST_GROUP_BY as g (g)}
        <option value={g}>{g}</option>
      {/each}
    </select>
  </label>
  <div class="col-span-3 flex justify-end">
    <button
      type="button"
      onclick={async () => {
        await Promise.all([loadSummary(), loadLog(true)]);
      }}
      disabled={summaryLoading || logLoading}
      class="px-4 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
      style="background: var(--accent-green); color: var(--text-inverse);"
    >
      {summaryLoading || logLoading ? 'Loading…' : 'Refresh'}
    </button>
  </div>
</section>

<section
  class="p-4 rounded mb-4"
  style="background: var(--bg-card); border: 1px solid var(--border-soft);"
>
  <h2 class="text-xs font-semibold uppercase mb-3" style="color: var(--text-muted);">
    Rollup by {groupBy}
  </h2>

  {#if summaryError}
    <p class="text-xs" style="color: var(--accent-red);">{summaryError}</p>
  {:else if summary === null}
    <p class="text-xs italic" style="color: var(--text-muted);">Loading…</p>
  {:else}
    <CostBars rows={summary.rows} keyLabel={groupBy} />

    <div
      class="mt-4 pt-3 flex items-center justify-between text-xs font-mono"
      style="border-top: 1px solid var(--border-soft); color: var(--text-secondary);"
    >
      <span>
        <span style="color: var(--text-muted);">total in window:</span>
        ${(summary.total_cents / 100).toFixed(4)}
      </span>
      <span>
        <span style="color: var(--text-muted);">projected monthly:</span>
        {projection === null ? '—' : `$${projection.toFixed(2)}`}
      </span>
    </div>
  {/if}
</section>

<section
  class="p-4 rounded"
  style="background: var(--bg-card); border: 1px solid var(--border-soft);"
>
  <h2 class="text-xs font-semibold uppercase mb-3" style="color: var(--text-muted);">
    Recent runs
  </h2>

  {#if logError}
    <p class="text-xs" style="color: var(--accent-red);">{logError}</p>
  {:else if log === null}
    <p class="text-xs italic" style="color: var(--text-muted);">Loading…</p>
  {:else if log.items.length === 0}
    <p class="text-xs italic" style="color: var(--text-muted);">No runs in this window.</p>
  {:else}
    <table class="w-full text-sm">
      <thead>
        <tr style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;">
          <th class="text-left px-3 py-2 font-semibold">ts</th>
          <th class="text-left px-3 py-2 font-semibold">op</th>
          <th class="text-left px-3 py-2 font-semibold">backend</th>
          <th class="text-right px-3 py-2 font-semibold">cents</th>
          <th class="text-right px-3 py-2 font-semibold">in</th>
          <th class="text-right px-3 py-2 font-semibold">out</th>
          <th class="text-right px-3 py-2 font-semibold">sec</th>
        </tr>
      </thead>
      <tbody>
        {#each log.items as item (item.id)}
          <tr style="border-bottom: 1px solid var(--border-soft);">
            <td class="px-3 py-2 text-xs font-mono" style="color: var(--text-muted);">
              {new Date(item.ts).toLocaleString()}
            </td>
            <td class="px-3 py-2 text-xs font-mono" style="color: var(--text-primary);">{item.op_name}</td>
            <td class="px-3 py-2 text-xs font-mono" style="color: var(--text-secondary);">
              {item.backend_name ?? '—'}
            </td>
            <td class="px-3 py-2 text-right text-xs font-mono">{item.actual_cents.toFixed(4)}</td>
            <td class="px-3 py-2 text-right text-xs font-mono" style="color: var(--text-muted);">{item.tokens_in}</td>
            <td class="px-3 py-2 text-right text-xs font-mono" style="color: var(--text-muted);">{item.tokens_out}</td>
            <td class="px-3 py-2 text-right text-xs font-mono" style="color: var(--text-muted);">
              {item.duration_seconds === null ? '—' : item.duration_seconds.toFixed(2)}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    {#if log.next_offset !== null}
      <div class="mt-3 text-center">
        <button
          type="button"
          disabled={logLoading}
          onclick={() => void loadMore()}
          class="px-3 py-1.5 text-xs rounded font-semibold disabled:opacity-50"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
        >
          {logLoading ? 'Loading…' : 'Load more'}
        </button>
      </div>
    {/if}
  {/if}
</section>
