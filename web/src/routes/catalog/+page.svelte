<script lang="ts">
  import { onMount } from 'svelte';
  import { api, ApiError } from '$lib/api/client';
  import { ARTIFACT_KINDS, type Artifact, type ArtifactKind, type ArtifactPage } from '$lib/api/artifacts';

  let kindFilter: ArtifactKind | 'all' = $state('all');
  let items: Artifact[] = $state([]);
  let nextOffset: number | null = $state(null);
  let error: string | null = $state(null);
  let loading = $state(false);

  async function load(): Promise<void> {
    loading = true;
    error = null;
    try {
      const qs = new URLSearchParams({ limit: '50' });
      if (kindFilter !== 'all') qs.set('kind', kindFilter);
      const page = await api.get<ArtifactPage>(`/artifacts?${qs.toString()}`);
      items = page.items;
      nextOffset = page.next_offset;
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      loading = false;
    }
  }

  async function loadMore(): Promise<void> {
    if (nextOffset === null) return;
    loading = true;
    try {
      const qs = new URLSearchParams({
        limit: '50',
        offset: String(nextOffset),
      });
      if (kindFilter !== 'all') qs.set('kind', kindFilter);
      const page = await api.get<ArtifactPage>(`/artifacts?${qs.toString()}`);
      items = [...items, ...page.items];
      nextOffset = page.next_offset;
    } finally {
      loading = false;
    }
  }

  onMount(() => void load());
</script>

<svelte:head>
  <title>media_engine · Catalog</title>
</svelte:head>

<header class="mb-5">
  <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Catalog</h1>
  <p class="text-sm" style="color: var(--text-secondary);">
    Every artifact in the cache. Filter by kind; tap a row to see the typed preview + lineage.
  </p>
</header>

<div class="flex flex-wrap gap-1 mb-4">
  <button
    type="button"
    onclick={() => {
      kindFilter = 'all';
      void load();
    }}
    class="px-3 py-1 text-xs font-mono rounded"
    style={kindFilter === 'all'
      ? 'background: var(--accent-green-soft); color: var(--accent-green); border: 1px solid var(--accent-green-line);'
      : 'background: var(--bg-card); color: var(--text-secondary); border: 1px solid var(--border-light);'}
  >
    all
  </button>
  {#each ARTIFACT_KINDS as kind (kind)}
    <button
      type="button"
      onclick={() => {
        kindFilter = kind;
        void load();
      }}
      class="px-3 py-1 text-xs font-mono rounded"
      style={kindFilter === kind
        ? 'background: var(--accent-green-soft); color: var(--accent-green); border: 1px solid var(--accent-green-line);'
        : 'background: var(--bg-card); color: var(--text-secondary); border: 1px solid var(--border-light);'}
    >
      {kind}
    </button>
  {/each}
</div>

{#if error}
  <p
    class="mb-4 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{error}</p>
{/if}

<table class="w-full text-sm" style="background: var(--bg-card); border: 1px solid var(--border-soft); border-radius: 4px;">
  <thead>
    <tr style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;">
      <th class="text-left px-3 py-2 font-semibold">id</th>
      <th class="text-left px-3 py-2 font-semibold">kind</th>
      <th class="text-left px-3 py-2 font-semibold">created</th>
      <th class="text-left px-3 py-2 font-semibold">derived from</th>
    </tr>
  </thead>
  <tbody>
    {#each items as art (art.id)}
      <tr style="border-bottom: 1px solid var(--border-soft);">
        <td class="px-3 py-2 font-mono text-xs">
          <a href="/catalog/{art.id}" style="color: var(--text-primary);">{art.id.slice(0, 12)}…</a>
        </td>
        <td class="px-3 py-2 text-xs font-mono">{art.kind}</td>
        <td class="px-3 py-2 text-xs font-mono" style="color: var(--text-muted);">
          {new Date(art.created_at).toLocaleString()}
        </td>
        <td class="px-3 py-2 text-xs font-mono" style="color: var(--text-muted);">
          {art.derived_from.length === 0 ? '—' : `${art.derived_from.length} parent${art.derived_from.length === 1 ? '' : 's'}`}
        </td>
      </tr>
    {/each}
    {#if items.length === 0 && !loading && !error}
      <tr>
        <td colspan="4" class="px-3 py-6 text-center text-xs italic" style="color: var(--text-muted);">
          No artifacts in this namespace yet.
        </td>
      </tr>
    {/if}
  </tbody>
</table>

{#if nextOffset !== null}
  <div class="mt-3 text-center">
    <button
      type="button"
      disabled={loading}
      onclick={() => void loadMore()}
      class="px-3 py-1.5 text-xs rounded font-semibold disabled:opacity-50"
      style="background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-light);"
    >
      {loading ? 'Loading…' : 'Load more'}
    </button>
  </div>
{/if}
