<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { ApiError } from '$lib/api/client';
  import { listProfiles, saveProfile } from '$lib/profile/api';
  import type { ProfileSummary } from '$lib/profile/types';
  import { BLANK_PIPELINE_YAML } from '$lib/profile/types';
  import { parseProfileText } from '$lib/profile/parse';

  let profiles = $state<ProfileSummary[]>([]);
  let error = $state<string | null>(null);
  let loading = $state(false);

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

  function isBundled(path: string): boolean {
    // Bundled profiles live under `<repo>/profiles/`; user profiles
    // under `{config_dir}/profiles/`. We don't know either path
    // exactly here, but the user dir is the only one that can
    // contain `/config/`.
    return !path.includes('/config/');
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
      await goto(`/profiles/${encodeURIComponent(name)}`);
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    }
  }
</script>

<svelte:head>
  <title>media_engine · Profiles</title>
</svelte:head>

<header class="mb-5 flex items-end justify-between">
  <div>
    <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Profiles</h1>
    <p class="text-sm" style="color: var(--text-secondary);">
      Pipelines + prompts the engine can run. Bundled profiles are read-only; user profiles can be
      edited or deleted.
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
      <a
        href="/profiles/{encodeURIComponent(p.name)}"
        class="block rounded p-4 no-underline"
        style="background: var(--bg-card); border: 1px solid var(--border-soft); color: var(--text-primary);"
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
            {p.description.length > 200 ? `${p.description.slice(0, 200)}…` : p.description}
          </p>
        {/if}
        <div class="mt-3 text-xs font-mono truncate" style="color: var(--text-muted);">
          {isBundled(p.path) ? '🔒 bundled' : '✎ user'} · {p.path.split('/').slice(-2).join('/')}
        </div>
      </a>
    {/each}
  </div>
{/if}
