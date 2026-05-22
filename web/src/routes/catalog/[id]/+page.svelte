<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { api, ApiError } from '$lib/api/client';
  import { artifactFileUrl, type Artifact } from '$lib/api/artifacts';
  import ArtifactPreview from '$lib/components/previews/ArtifactPreview.svelte';

  type LineageNode = {
    artifact_id: string;
    kind: string;
    op?: string | null;
    backend?: string | null;
    truncated_reason?: string | null;
    inputs?: LineageNode[];
  };

  let artifact: Artifact | null = $state(null);
  let lineage: LineageNode | null = $state(null);
  let error: string | null = $state(null);
  let activeTab: 'preview' | 'metadata' | 'lineage' = $state('preview');
  const artifactId = $derived($page.params.id ?? '');

  async function load(): Promise<void> {
    try {
      artifact = await api.get<Artifact>(`/artifacts/${artifactId}`);
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
      return;
    }
    try {
      lineage = await api.get<LineageNode>(`/artifacts/${artifactId}/lineage?depth=6`);
    } catch (e) {
      // Lineage is best-effort; the artifact itself is the main view.
      lineage = null;
    }
  }

  onMount(() => void load());
</script>

<svelte:head>
  <title>media_engine · {artifactId.slice(0, 8)}</title>
</svelte:head>

<header class="mb-5">
  <p class="text-xs font-mono mb-1" style="color: var(--text-muted);">
    <a href="/catalog">catalog</a> / {artifactId}
  </p>
  {#if artifact}
    <h1 class="text-2xl font-semibold mb-1 font-mono" style="color: var(--text-primary);">
      {artifact.kind}
    </h1>
    <div class="flex gap-3 text-xs">
      <span style="color: var(--text-muted);">
        ns: <code class="font-mono">{artifact.namespace}</code>
      </span>
      <span style="color: var(--text-muted);">
        created {new Date(artifact.created_at).toLocaleString()}
      </span>
      <a
        href={artifactFileUrl(artifact.id)}
        download
        style="color: var(--accent-green);"
      >
        Download
      </a>
    </div>
  {/if}
</header>

{#if error}
  <p
    class="mb-4 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{error}</p>
{/if}

{#if artifact}
  <div class="flex gap-1 mb-4 text-sm">
    {#each [
      { id: 'preview', label: 'Preview' },
      { id: 'metadata', label: 'Metadata' },
      { id: 'lineage', label: 'Lineage' },
    ] as tab (tab.id)}
      <button
        type="button"
        onclick={() => (activeTab = tab.id as typeof activeTab)}
        class="px-3 py-1.5 rounded font-medium"
        style={activeTab === tab.id
          ? 'background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-light);'
          : 'color: var(--text-secondary); border: 1px solid transparent;'}
      >
        {tab.label}
      </button>
    {/each}
  </div>

  {#if activeTab === 'preview'}
    <ArtifactPreview {artifact} />
  {:else if activeTab === 'metadata'}
    <pre
      class="font-mono text-xs whitespace-pre-wrap p-4 rounded"
      style="background: var(--bg-card); border: 1px solid var(--border-soft); color: var(--text-primary);"
    >{JSON.stringify(artifact, null, 2)}</pre>
  {:else if activeTab === 'lineage'}
    <section
      class="p-4 rounded text-xs"
      style="background: var(--bg-card); border: 1px solid var(--border-soft);"
    >
      <p style="color: var(--text-muted);">
        Visual graph viewer lands in commit 45. Plain tree below.
      </p>
      {#if lineage}
        {#snippet renderNode(node: LineageNode, depth: number)}
          <div class="font-mono" style="padding-left: {depth * 1.5}rem;">
            <span style="color: var(--text-muted);">{node.kind}</span>
            <a href="/catalog/{node.artifact_id}" style="color: var(--text-primary);">
              {node.artifact_id.slice(0, 12)}…
            </a>
            {#if node.op}
              <span style="color: var(--text-muted);">
                · {node.op}{node.backend ? ` · ${node.backend}` : ''}
              </span>
            {/if}
            {#if node.truncated_reason}
              <span style="color: var(--accent-amber);">
                … ({node.truncated_reason})
              </span>
            {/if}
          </div>
          {#each node.inputs ?? [] as child, i (child.artifact_id + i)}
            {@render renderNode(child, depth + 1)}
          {/each}
        {/snippet}
        {@render renderNode(lineage, 0)}
      {:else}
        <p style="color: var(--text-muted);">No lineage information.</p>
      {/if}
    </section>
  {/if}
{/if}
