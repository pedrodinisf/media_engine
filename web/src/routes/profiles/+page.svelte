<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { ApiError } from '$lib/api/client';
  import {
    getProfile,
    listProfiles,
    forkPayload,
    saveProfile,
  } from '$lib/profile/api';
  import type {
    PipelineProfile,
    ProfileSummary,
    PromptProfile,
  } from '$lib/profile/types';
  import { BLANK_PIPELINE_YAML } from '$lib/profile/types';
  import { parseProfileText } from '$lib/profile/parse';
  import ForkModal from '$lib/components/profile/ForkModal.svelte';

  let profiles = $state<ProfileSummary[]>([]);
  let error = $state<string | null>(null);
  let loading = $state(false);

  // Lazy-loaded body previews — fetched once per card when the user
  // hits "Show body". Keyed by profile name.
  let bodyPreviews = $state<Record<string, string>>({});
  let bodyLoading = $state<Record<string, boolean>>({});

  let forkSource = $state<{
    name: string;
    body: PipelineProfile | PromptProfile;
  } | null>(null);
  let forking = $state(false);

  async function load(): Promise<void> {
    loading = true;
    error = null;
    try {
      profiles = await listProfiles();
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      loading = false;
    }
  }

  onMount(() => void load());

  function isBundled(p: ProfileSummary): boolean {
    // Server-supplied (commit 47 audit). The pre-audit heuristic of
    // sniffing `/config/` out of the path was wrong for non-default
    // config dirs; the route now stamps a `source` field per row.
    return p.source === 'bundled';
  }

  function bodyExcerpt(body: PipelineProfile | PromptProfile): string {
    if (body.kind === 'prompt') {
      // Prompt bodies are markdown — strip nothing, just trim + slice.
      return body.body.split('\n').slice(0, 30).join('\n').trim();
    }
    // Pipeline body = the YAML serialisation of the graph node list,
    // which the workspace renders in full. The card shows the first
    // 30 lines for a flavour-only excerpt.
    const lines: string[] = [];
    for (const node of body.graph.slice(0, 6)) {
      lines.push(`- id: ${node.id}`);
      lines.push(`  op: ${node.op}`);
      if (node.backend) lines.push(`  backend: ${node.backend}`);
    }
    return lines.slice(0, 30).join('\n');
  }

  async function loadBodyPreview(name: string): Promise<void> {
    if (bodyPreviews[name] !== undefined || bodyLoading[name]) return;
    bodyLoading = { ...bodyLoading, [name]: true };
    try {
      const body = await getProfile(name);
      bodyPreviews = { ...bodyPreviews, [name]: bodyExcerpt(body) };
    } catch (e) {
      bodyPreviews = {
        ...bodyPreviews,
        [name]: `(failed to load: ${e instanceof ApiError ? e.detail : String(e)})`,
      };
    } finally {
      bodyLoading = { ...bodyLoading, [name]: false };
    }
  }

  async function startFork(p: ProfileSummary): Promise<void> {
    try {
      const body = await getProfile(p.name);
      // Strip the path-only metadata field the GET appends.
      const { _source_path: _ignored, ...clean } = body;
      void _ignored;
      forkSource = { name: p.name, body: clean as PipelineProfile | PromptProfile };
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    }
  }

  async function confirmFork(newName: string): Promise<void> {
    if (!forkSource) return;
    forking = true;
    error = null;
    try {
      const payload = forkPayload(forkSource.body, newName);
      await saveProfile(payload);
      forkSource = null;
      await goto(`${base}/profiles/${encodeURIComponent(newName)}`);
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      forking = false;
    }
  }

  async function createBlank(): Promise<void> {
    // Generate a unique name suffix so successive "New" clicks don't
    // collide. The server-side kebab-regex restricts the chars; we
    // pad with a short timestamp.
    const stamp = Date.now().toString(36).slice(-5);
    const name = `untitled-${stamp}`;
    const yaml = BLANK_PIPELINE_YAML.replace(/^name: .*$/m, `name: ${name}`);
    const parsed = parseProfileText(yaml);
    if (parsed.kind !== 'pipeline') return;
    try {
      await saveProfile({
        profile_schema_version: '1.0',
        name,
        kind: 'pipeline',
        description: parsed.description,
        inputs: parsed.inputs,
        graph: parsed.nodes,
        outputs: parsed.outputs,
      });
      await goto(`${base}/profiles/${encodeURIComponent(name)}`);
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    }
  }

  const existingUserNames = $derived(
    profiles.filter((p) => !isBundled(p)).map((p) => p.name),
  );
</script>

<svelte:head>
  <title>media_engine · Profiles</title>
</svelte:head>

<header class="mb-5 flex items-end justify-between">
  <div>
    <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Profiles</h1>
    <p class="text-sm" style="color: var(--text-secondary);">
      Pipelines + prompts the engine can run. Bundled profiles are read-only — fork to a
      user-editable copy with one click.
    </p>
  </div>
  <button
    type="button"
    onclick={() => void createBlank()}
    class="px-4 py-1.5 rounded text-xs font-semibold"
    style="background: var(--accent-green); color: var(--text-inverse);"
  >
    + New pipeline
  </button>
</header>

{#if error}
  <p
    class="mb-4 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{error}</p>
{/if}

{#if loading}
  <p class="text-xs italic" style="color: var(--text-muted);">Loading…</p>
{:else if profiles.length === 0}
  <p class="text-sm italic" style="color: var(--text-muted);">
    No profiles yet. Click <strong>+ New pipeline</strong> to start one, or drop YAML / Markdown
    files into <code class="font-mono text-xs">~/.config/media_engine/profiles/</code>.
  </p>
{:else}
  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
    {#each profiles as p (p.name)}
      {@const bundled = isBundled(p)}
      {@const preview = bodyPreviews[p.name]}
      {@const isPreviewLoading = bodyLoading[p.name] === true}
      <article
        class="rounded p-4 flex flex-col"
        style="background: var(--bg-card); border: 1px solid var(--border-soft); color: var(--text-primary);"
      >
        <a
          href="{base}/profiles/{encodeURIComponent(p.name)}"
          class="no-underline"
          style="color: inherit;"
        >
          <div class="flex items-baseline justify-between mb-1">
            <span class="font-mono text-sm font-semibold">{p.name}</span>
            <span
              class="text-xs font-mono px-1.5 py-0.5 rounded"
              style="background: {p.kind === 'pipeline'
                ? 'var(--accent-green-soft)'
                : 'var(--accent-amber-soft)'}; color: {p.kind === 'pipeline'
                ? 'var(--accent-green)'
                : 'var(--accent-amber)'};"
            >{p.kind}</span>
          </div>
          {#if p.description}
            <p class="text-xs mt-1" style="color: var(--text-secondary);">
              {p.description.length > 240 ? `${p.description.slice(0, 240)}…` : p.description}
            </p>
          {/if}
        </a>

        {#if preview !== undefined}
          <pre
            class="mt-2 text-xs font-mono p-2 rounded max-h-48 overflow-y-auto whitespace-pre-wrap"
            style="background: var(--bg-page); color: var(--text-muted); border: 1px solid var(--border-soft);"
          >{preview}</pre>
        {/if}

        <div
          class="mt-3 pt-2 flex items-center justify-between gap-2 text-xs"
          style="border-top: 1px solid var(--border-soft);"
        >
          <span class="font-mono truncate" style="color: var(--text-muted);">
            {bundled ? '🔒 bundled' : '✎ user'} · {p.path.split('/').slice(-2).join('/')}
          </span>
          <div class="flex items-center gap-1.5 shrink-0">
            <button
              type="button"
              onclick={() => void loadBodyPreview(p.name)}
              disabled={isPreviewLoading || preview !== undefined}
              class="px-2 py-1 rounded font-mono disabled:opacity-50"
              style="background: var(--bg-page); color: var(--text-secondary); border: 1px solid var(--border-light);"
              title="Show the first ~30 lines of the body / graph"
            >
              {preview !== undefined ? 'shown' : isPreviewLoading ? '…' : 'body'}
            </button>
            {#if bundled}
              <button
                type="button"
                onclick={() => void startFork(p)}
                class="px-2 py-1 rounded font-mono font-semibold"
                style="background: var(--accent-green-soft); color: var(--accent-green); border: 1px solid var(--accent-green-line);"
                title="Copy to your config dir and open the editable copy"
              >
                fork
              </button>
            {/if}
          </div>
        </div>
      </article>
    {/each}
  </div>
{/if}

{#if forkSource}
  <ForkModal
    sourceName={forkSource.name}
    existingNames={existingUserNames}
    onCancel={() => (forkSource = null)}
    onConfirm={confirmFork}
  />
  {#if forking}
    <div
      class="fixed inset-x-0 bottom-4 z-50 flex justify-center"
      aria-hidden="true"
    >
      <span
        class="rounded px-3 py-1.5 text-xs"
        style="background: var(--bg-card); color: var(--text-secondary); border: 1px solid var(--border-soft);"
      >
        Forking…
      </span>
    </div>
  {/if}
{/if}
