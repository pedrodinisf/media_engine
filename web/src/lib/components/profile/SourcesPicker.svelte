<script lang="ts">
  /**
   * Modal for binding profile-declared inputs to in-namespace
   * artifacts before submitting a `POST /pipelines` run. The user
   * sees one section per declared input; each section lists artifacts
   * of the declared kind from `GET /artifacts?kind=...`.
   */
  import { onMount } from 'svelte';
  import { ApiError } from '$lib/api/client';
  import type { Artifact, ArtifactPage } from '$lib/api/artifacts';
  import { api } from '$lib/api/client';
  import type { InputSpec } from '$lib/profile/types';

  type Props = {
    inputs: readonly InputSpec[];
    onCancel: () => void;
    onConfirm: (selection: Record<string, string>) => void;
  };
  let { inputs, onCancel, onConfirm }: Props = $props();

  // Map of declared-input-name → artifact id.
  let selection = $state<Record<string, string>>({});
  // Map of kind → preloaded list of artifacts (for the dropdown).
  let optionsByKind = $state<Record<string, Artifact[]>>({});
  let loading = $state(true);
  let error = $state<string | null>(null);

  async function loadOptions(): Promise<void> {
    loading = true;
    error = null;
    try {
      const kinds = [...new Set(inputs.map((i) => i.kind))];
      const result = await Promise.all(
        kinds.map(async (kind) => {
          const page = await api.get<ArtifactPage>(`/artifacts?kind=${encodeURIComponent(kind)}&limit=100`);
          return [kind, page.items] as const;
        }),
      );
      optionsByKind = Object.fromEntries(result);
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      loading = false;
    }
  }

  onMount(() => void loadOptions());

  const allBound = $derived(inputs.every((i) => !!selection[i.name]));

  function submit(): void {
    if (allBound) onConfirm({ ...selection });
  }
</script>

<div
  class="fixed inset-0 z-50 flex items-center justify-center p-6"
  style="background: rgba(0,0,0,0.4);"
  onclick={onCancel}
  onkeydown={(e) => e.key === 'Escape' && onCancel()}
  role="presentation"
>
  <div
    class="rounded p-5 w-full max-w-2xl"
    style="background: var(--bg-card); border: 1px solid var(--border-soft);"
    onclick={(e) => e.stopPropagation()}
    onkeydown={(e) => e.stopPropagation()}
    role="dialog"
    aria-modal="true"
    aria-labelledby="sources-picker-title"
    tabindex={-1}
  >
    <h2 id="sources-picker-title" class="text-sm font-semibold uppercase mb-3" style="color: var(--text-secondary);">
      Bind sources before running
    </h2>

    {#if loading}
      <p class="text-xs italic" style="color: var(--text-muted);">Loading artifacts…</p>
    {:else if error}
      <p class="text-xs" style="color: var(--accent-red);">{error}</p>
    {:else}
      <div class="grid grid-cols-1 gap-3 max-h-[60vh] overflow-y-auto">
        {#each inputs as inp (inp.name)}
          {@const options = optionsByKind[inp.kind] ?? []}
          <label class="block">
            <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">
              {inp.name} <span class="font-normal" style="color: var(--text-muted);">({inp.kind})</span>
            </span>
            {#if options.length === 0}
              <p class="text-xs italic" style="color: var(--text-muted);">
                No artifacts of kind <code class="font-mono">{inp.kind}</code> in this namespace.
                Ingest one first via /ui/ingest.
              </p>
            {:else}
              <select
                bind:value={selection[inp.name]}
                class="w-full px-3 py-2 rounded text-xs font-mono"
                style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
              >
                <option value="">— pick one —</option>
                {#each options as opt (opt.id)}
                  <option value={opt.id}>
                    {opt.id.slice(0, 12)}… &middot;
                    {new Date(opt.created_at).toLocaleDateString()}
                  </option>
                {/each}
              </select>
            {/if}
          </label>
        {/each}
      </div>
    {/if}

    <div class="mt-4 pt-3 flex items-center justify-end gap-2" style="border-top: 1px solid var(--border-soft);">
      <button
        type="button"
        onclick={onCancel}
        class="px-3 py-1.5 rounded text-xs"
        style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      >
        Cancel
      </button>
      <button
        type="button"
        disabled={!allBound || loading}
        onclick={submit}
        class="px-4 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
        style="background: var(--accent-green); color: var(--text-inverse);"
      >
        Run pipeline
      </button>
    </div>
  </div>
</div>
