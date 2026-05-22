<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { setToken } from '$lib/stores/token';

  let secret = $state('');
  let rememberMe = $state(true);
  let error: string | null = $state(null);
  let submitting = $state(false);

  // Cheap shape check — bearer tokens are 32-byte url-safe secrets
  // (commit 29). Server-side `verify_bearer` is the real authority; this
  // just catches the obvious "you pasted the wrong line" mistake.
  function looksLikeToken(value: string): boolean {
    const trimmed = value.trim();
    return /^[A-Za-z0-9_-]{20,}$/.test(trimmed);
  }

  async function submit(): Promise<void> {
    const trimmed = secret.trim();
    if (!looksLikeToken(trimmed)) {
      error = 'That doesn\'t look like a bearer token. It should be a long url-safe string from `med api token create`.';
      return;
    }
    submitting = true;
    error = null;
    try {
      // Verify against the API before committing it to storage — if the
      // user pasted yesterday's revoked token, we'd rather catch it here
      // than after every subsequent request 401s. NOTE: ``/operations``
      // is a REST path, NOT scoped to the SPA's ``paths.base`` ('/ui') —
      // FastAPI serves it at the app root. Keep this absolute.
      const res = await fetch('/operations', {
        headers: { Authorization: `Bearer ${trimmed}` },
      });
      if (res.status === 401) {
        error = 'The API rejected this token (401). Mint a fresh one with `med api token create`.';
        submitting = false;
        return;
      }
      if (!res.ok) {
        error = `API returned ${res.status}. Is the engine running on this host?`;
        submitting = false;
        return;
      }
      setToken(trimmed, rememberMe ? 'local' : 'session');
      await goto(`${base}/`);
    } catch (e) {
      error = `Could not reach the API: ${e instanceof Error ? e.message : String(e)}`;
      submitting = false;
    }
  }
</script>

<svelte:head>
  <title>media_engine · Setup</title>
</svelte:head>

<section class="max-w-xl mx-auto py-10">
  <h1 class="text-2xl font-semibold mb-2" style="color: var(--text-primary);">
    Connect to your engine
  </h1>
  <p class="text-sm mb-6" style="color: var(--text-secondary);">
    The Web UI talks to a running <code class="font-mono text-xs px-1.5 py-0.5 rounded" style="background: var(--bg-alt); border: 1px solid var(--border-soft);">med api start</code>
    on the same machine. Mint a bearer token in your shell, then paste it here.
  </p>

  <div
    class="p-4 mb-5 rounded"
    style="background: var(--bg-alt); border: 1px solid var(--border-soft);"
  >
    <p class="text-xs mb-2 font-semibold" style="color: var(--text-secondary);">
      In your terminal:
    </p>
    <pre
      class="font-mono text-xs p-3 rounded overflow-x-auto"
      style="background: var(--bg-deep); color: var(--text-primary); border: 1px solid var(--border-warm);"
    ><code>med api token create --label web-ui</code></pre>
    <p class="text-xs mt-2" style="color: var(--text-muted);">
      The secret prints once. Paste it below.
    </p>
  </div>

  <form
    onsubmit={(e) => {
      e.preventDefault();
      void submit();
    }}
  >
    <label class="block mb-1 text-xs font-semibold" for="token" style="color: var(--text-secondary);">
      Bearer token
    </label>
    <input
      id="token"
      type="password"
      bind:value={secret}
      autocomplete="off"
      spellcheck="false"
      autocapitalize="off"
      class="w-full px-3 py-2 rounded font-mono text-sm"
      style="background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-light);"
      placeholder="paste the secret here"
      disabled={submitting}
    />

    <label class="flex items-center gap-2 mt-3 text-xs" style="color: var(--text-secondary);">
      <input
        type="checkbox"
        bind:checked={rememberMe}
        disabled={submitting}
      />
      Remember me on this device (uncheck for session-only storage)
    </label>

    {#if error}
      <p
        class="mt-3 text-xs p-2 rounded"
        style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
      >
        {error}
      </p>
    {/if}

    <button
      type="submit"
      disabled={submitting || !secret.trim()}
      class="mt-4 px-4 py-2 rounded font-semibold text-sm disabled:opacity-50"
      style="background: var(--accent-green); color: var(--text-inverse);"
    >
      {submitting ? 'Verifying…' : 'Connect'}
    </button>
  </form>

  <p class="mt-6 text-xs" style="color: var(--text-muted);">
    Token storage: <span class="font-mono">{rememberMe ? 'localStorage' : 'sessionStorage'}</span>.
    The secret is XSS-readable by design (SSE needs to pass it via query string).
    See plan §13 for the v1.x hardening path.
  </p>
</section>
