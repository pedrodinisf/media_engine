<script lang="ts">
  /**
   * Modal asking for a new kebab-case name when forking a bundled
   * profile. Validates client-side against `PROFILE_NAME_RE` to give
   * instant feedback; the server enforces the same regex.
   */
  import { untrack } from 'svelte';
  import { isValidProfileName, PROFILE_NAME_RE } from '$lib/profile/api';

  type Props = {
    sourceName: string;
    /** Existing user-profile names — used to warn before overwriting. */
    existingNames: readonly string[];
    onCancel: () => void;
    onConfirm: (newName: string) => void;
  };
  let { sourceName, existingNames, onCancel, onConfirm }: Props = $props();

  // Default to `<source>-fork` so a one-click confirm is meaningful.
  // `untrack` reads the prop at component-init without subscribing,
  // silencing svelte/state_referenced_locally — the parent re-mounts
  // the modal each time the user starts a fork, so we never need to
  // react to a mid-modal sourceName change.
  let candidate = $state(untrack(() => `${sourceName}-fork`));

  const isValid = $derived(isValidProfileName(candidate));
  const collides = $derived(existingNames.includes(candidate));

  function submit(): void {
    if (isValid) onConfirm(candidate);
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
    class="rounded p-5 w-full max-w-md"
    style="background: var(--bg-card); border: 1px solid var(--border-soft);"
    onclick={(e) => e.stopPropagation()}
    onkeydown={(e) => e.stopPropagation()}
    role="dialog"
    aria-modal="true"
    aria-labelledby="fork-modal-title"
    tabindex={-1}
  >
    <h2
      id="fork-modal-title"
      class="text-sm font-semibold uppercase mb-3"
      style="color: var(--text-secondary);"
    >
      Fork <span class="font-mono">{sourceName}</span>
    </h2>

    <p class="text-xs mb-3" style="color: var(--text-muted);">
      Copies the profile to <code class="font-mono">{'~/.config/media_engine/profiles/'}</code>
      so you can edit it. The bundled original stays unchanged.
    </p>

    <label class="block">
      <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">
        New name
      </span>
      <input
        type="text"
        bind:value={candidate}
        class="w-full px-3 py-2 rounded text-sm font-mono"
        style="background: var(--bg-page); color: var(--text-primary); border: 1px solid {isValid ? 'var(--border-light)' : 'rgba(220,38,38,0.45)'};"
        placeholder="my-pipeline"
        autocomplete="off"
        spellcheck="false"
      />
    </label>

    {#if !isValid}
      <p class="mt-2 text-xs" style="color: var(--accent-red);">
        Must match <code class="font-mono">{PROFILE_NAME_RE.source}</code>: lowercase letters /
        digits / <code class="font-mono">-</code> / <code class="font-mono">_</code>, starting
        with a letter or digit, 1–64 chars.
      </p>
    {:else if collides}
      <p class="mt-2 text-xs" style="color: var(--accent-amber);">
        Heads up — a user profile by that name already exists. Forking will overwrite it.
      </p>
    {/if}

    <div
      class="mt-4 pt-3 flex items-center justify-end gap-2"
      style="border-top: 1px solid var(--border-soft);"
    >
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
        disabled={!isValid}
        onclick={submit}
        class="px-4 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
        style="background: var(--accent-green); color: var(--text-inverse);"
      >
        Fork
      </button>
    </div>
  </div>
</div>
