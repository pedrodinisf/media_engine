<script lang="ts">
  import { base } from '$app/paths';
  import { ApiError } from '$lib/api/client';
  import { ARTIFACT_KINDS, type ArtifactKind } from '$lib/api/artifacts';
  import {
    SEARCH_MODES,
    search as runSearch,
    type SearchMode,
    type SearchResponse,
  } from '$lib/api/search';

  let query = $state('');
  let mode = $state<SearchMode>('fulltext');
  let topK = $state(10);
  let kindFilter = $state<ArtifactKind | 'all'>('all');

  let response = $state<SearchResponse | null>(null);
  let error = $state<string | null>(null);
  let busy = $state(false);

  // Debounced live search — every keystroke schedules a call 300 ms
  // later, but the in-flight result is dropped if the user keeps
  // typing (or the component unmounts). Mirrors the cost-preview
  // pattern in routes/run/+page.svelte; the cancelled flag prevents
  // late responses from writing into dead state.
  $effect(() => {
    const _q = query.trim();
    const _mode = mode;
    const _topK = topK;
    const _kind = kindFilter;
    if (_q === '') {
      response = null;
      error = null;
      busy = false;
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      busy = true;
      error = null;
      try {
        const body: {
          mode: SearchMode;
          query: string;
          top_k: number;
          kind?: string;
        } = { mode: _mode, query: _q, top_k: _topK };
        if (_kind !== 'all') body.kind = _kind;
        const result = await runSearch(body);
        if (!cancelled) response = result;
      } catch (e) {
        if (!cancelled) {
          response = null;
          error = e instanceof ApiError ? e.detail : String(e);
        }
      } finally {
        if (!cancelled) busy = false;
      }
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  });
</script>

<svelte:head>
  <title>media_engine · Search</title>
</svelte:head>

<header class="mb-5">
  <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Search</h1>
  <p class="text-sm" style="color: var(--text-secondary);">
    Query the catalog. Fulltext is keyword-only; semantic + hybrid embed the query first (needs the
    <code class="font-mono text-xs">embed</code> extra).
  </p>
</header>

<section
  class="p-4 rounded mb-4"
  style="background: var(--bg-card); border: 1px solid var(--border-soft);"
>
  <label class="block mb-3">
    <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">Query</span>
    <input
      type="text"
      bind:value={query}
      placeholder="What are you looking for?"
      class="w-full px-3 py-2 rounded text-sm"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      autocomplete="off"
    />
  </label>

  <div class="grid grid-cols-12 gap-3 items-end">
    <label class="col-span-4">
      <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">Mode</span>
      <select
        bind:value={mode}
        class="w-full px-3 py-2 rounded text-sm font-mono"
        style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      >
        {#each SEARCH_MODES as m (m)}
          <option value={m}>{m}</option>
        {/each}
      </select>
    </label>

    <label class="col-span-4">
      <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">
        Top-k: <span class="font-mono">{topK}</span>
      </span>
      <input
        type="range"
        min="1"
        max="50"
        bind:value={topK}
        class="w-full"
      />
    </label>

    <label class="col-span-4">
      <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">Kind</span>
      <select
        bind:value={kindFilter}
        class="w-full px-3 py-2 rounded text-sm font-mono"
        style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      >
        <option value="all">all kinds</option>
        {#each ARTIFACT_KINDS as k (k)}
          <option value={k}>{k}</option>
        {/each}
      </select>
    </label>
  </div>
</section>

{#if error}
  <p
    class="mb-4 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{error}</p>
{/if}

{#if busy}
  <p class="text-xs italic" style="color: var(--text-muted);">Searching…</p>
{:else if response && response.results.length === 0 && query.trim() !== ''}
  <p class="text-sm italic" style="color: var(--text-muted);">
    No matches for <code class="font-mono">{response.query}</code> ({response.mode}).
  </p>
{:else if response}
  <p class="text-xs mb-2 font-mono" style="color: var(--text-muted);">
    {response.results.length} {response.results.length === 1 ? 'hit' : 'hits'} ·
    mode: {response.mode} · top-k: {response.top_k}
  </p>

  <ol
    class="text-sm rounded overflow-hidden"
    style="background: var(--bg-card); border: 1px solid var(--border-soft);"
  >
    {#each response.results as r, i (r.artifact_id)}
      <li
        style="border-bottom: {i === response.results.length - 1 ? 'none' : '1px solid var(--border-soft)'};"
      >
        <a
          href="{base}/catalog/{r.artifact_id}"
          class="block px-3 py-2"
          style="color: var(--text-primary); text-decoration: none;"
        >
          <div class="flex items-baseline gap-3">
            <span
              class="text-xs font-mono px-1.5 py-0.5 rounded"
              style="background: var(--accent-green-soft); color: var(--accent-green);"
            >{r.kind ?? '?'}</span>
            <span class="font-mono text-xs" style="color: var(--text-secondary);">{r.artifact_id.slice(0, 16)}…</span>
            <span class="ml-auto font-mono text-xs" style="color: var(--text-muted);">score {r.score.toFixed(4)}</span>
          </div>
          {#if r.snippet}
            <p class="mt-1 text-xs" style="color: var(--text-muted);">
              {r.snippet.length > 240 ? `${r.snippet.slice(0, 240)}…` : r.snippet}
            </p>
          {/if}
        </a>
      </li>
    {/each}
  </ol>
{:else if query.trim() === ''}
  <p class="text-sm italic" style="color: var(--text-muted);">
    Type a query to see hits live.
  </p>
{/if}
