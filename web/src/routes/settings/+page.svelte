<script lang="ts">
  /**
   * Settings panel — Phase 6 commit 49.
   *
   * Six tabs (per plan §5 commit 49): Tokens · Backends · Plugins
   * (Extras + Catalog) · Storage · Resources/Config (read-only).
   * Each tab is a small block; data fetches lazy on tab activation
   * so the initial paint stays cheap.
   */
  import { onMount } from 'svelte';
  import { ApiError, api } from '$lib/api/client';
  import {
    backendDetail,
    createToken,
    formatBytes,
    getCatalog,
    isRevoked,
    listBackends,
    listExtras,
    listTokens,
    putCatalog,
    revokeToken,
    storageGC,
    storageStats,
    type BackendSummary,
    type CatalogResponse,
    type ExtraRow,
    type GCResponse,
    type StorageStats,
    type TokenInfo,
  } from '$lib/api/settings';

  type Tab = 'tokens' | 'backends' | 'extras' | 'catalog' | 'storage' | 'config';
  const TABS: ReadonlyArray<{ id: Tab; label: string }> = [
    { id: 'tokens', label: 'Tokens' },
    { id: 'backends', label: 'Backends' },
    { id: 'extras', label: 'Plugins · Extras' },
    { id: 'catalog', label: 'Plugins · Catalog' },
    { id: 'storage', label: 'Storage' },
    { id: 'config', label: 'Config' },
  ];

  let activeTab = $state<Tab>('tokens');
  let error = $state<string | null>(null);

  // ─────────────── Tokens ───────────────
  let tokens = $state<TokenInfo[]>([]);
  let tokensLoading = $state(false);
  let newTokenLabel = $state('');
  let newTokenNamespace = $state('default');
  let newSecret = $state<string | null>(null);
  let tokenError = $state<string | null>(null);

  async function refreshTokens(): Promise<void> {
    tokensLoading = true;
    tokenError = null;
    try {
      tokens = await listTokens(true);
    } catch (e) {
      tokenError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      tokensLoading = false;
    }
  }

  async function submitNewToken(): Promise<void> {
    if (!newTokenLabel.trim()) {
      tokenError = 'Label required.';
      return;
    }
    tokenError = null;
    try {
      const result = await createToken(
        newTokenLabel.trim(),
        newTokenNamespace.trim() || 'default',
      );
      newSecret = result.secret;
      newTokenLabel = '';
      await refreshTokens();
    } catch (e) {
      tokenError = e instanceof ApiError ? e.detail : String(e);
    }
  }

  async function revokeOne(id: string): Promise<void> {
    if (!confirm(`Revoke token ${id.slice(0, 8)}…? This cannot be undone.`)) return;
    try {
      await revokeToken(id);
      await refreshTokens();
    } catch (e) {
      tokenError = e instanceof ApiError ? e.detail : String(e);
    }
  }

  // ─────────────── Backends ───────────────
  let backends = $state<BackendSummary[]>([]);
  let backendHealth = $state<Record<string, string>>({});
  let backendsLoading = $state(false);

  async function refreshBackends(): Promise<void> {
    backendsLoading = true;
    error = null;
    try {
      backends = await listBackends();
      // Probe health in parallel — best-effort; failed probes show ⚪.
      const probes = await Promise.all(
        backends.map(async (b) => {
          try {
            const detail = await backendDetail(b.name, b.op_name);
            return [`${b.op_name}__${b.name}`, detail.health] as const;
          } catch {
            return [`${b.op_name}__${b.name}`, 'unknown'] as const;
          }
        }),
      );
      backendHealth = Object.fromEntries(probes);
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      backendsLoading = false;
    }
  }

  function healthIcon(state: string | undefined): string {
    switch (state) {
      case 'ok': return '🟢';
      case 'degraded': return '🟡';
      case 'unavailable': return '🔴';
      default: return '⚪';
    }
  }

  // ─────────────── Plugins · Extras ───────────────
  let extras = $state<ExtraRow[]>([]);
  let extrasLoading = $state(false);
  let copiedCommand = $state<string | null>(null);

  async function refreshExtras(): Promise<void> {
    extrasLoading = true;
    try {
      const r = await listExtras();
      extras = r.items;
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      extrasLoading = false;
    }
  }

  async function copyCommand(cmd: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(cmd);
      copiedCommand = cmd;
      setTimeout(() => (copiedCommand = null), 1500);
    } catch {
      // Clipboard API may be unavailable on http (non-localhost); the
      // user can still select the text manually.
    }
  }

  // ─────────────── Plugins · Catalog gate ───────────────
  let catalog = $state<CatalogResponse | null>(null);
  let hiddenOps = $state<Set<string>>(new Set());
  let hiddenBackends = $state<Set<string>>(new Set());
  let catalogLoading = $state(false);
  let catalogSaving = $state(false);
  let catalogFilter = $state('');

  async function refreshCatalog(): Promise<void> {
    catalogLoading = true;
    try {
      catalog = await getCatalog();
      hiddenOps = new Set(catalog.hidden_ops);
      hiddenBackends = new Set(catalog.hidden_backends);
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      catalogLoading = false;
    }
  }

  function toggleOp(name: string): void {
    const next = new Set(hiddenOps);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    hiddenOps = next;
  }

  function toggleBackend(key: string): void {
    const next = new Set(hiddenBackends);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    hiddenBackends = next;
  }

  async function saveCatalog(): Promise<void> {
    catalogSaving = true;
    try {
      const next = await putCatalog([...hiddenOps], [...hiddenBackends]);
      catalog = next;
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      catalogSaving = false;
    }
  }

  // ─────────────── Storage ───────────────
  let stats = $state<StorageStats | null>(null);
  let statsLoading = $state(false);
  let gcResult = $state<GCResponse | null>(null);
  let gcRunning = $state(false);

  async function refreshStats(): Promise<void> {
    statsLoading = true;
    try {
      stats = await storageStats();
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      statsLoading = false;
    }
  }

  async function runGC(apply: boolean): Promise<void> {
    gcRunning = true;
    try {
      gcResult = await storageGC(apply);
      if (apply) await refreshStats();
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      gcRunning = false;
    }
  }

  // ─────────────── Config (read-only) ───────────────
  type OpDetail = {
    name: string;
    declared_resources: string[];
  };
  let opDetails = $state<OpDetail[]>([]);
  let opsLoading = $state(false);

  async function refreshConfig(): Promise<void> {
    opsLoading = true;
    try {
      const ops = await api.get<Array<{ name: string }>>('/operations');
      // Pull declared_resources per op so the Resources subsection
      // shows the effective resource semaphore allocation.
      const details = await Promise.all(
        ops.map(async (op) => {
          const detail = await api.get<OpDetail>(
            `/operations/${encodeURIComponent(op.name)}`,
          );
          return { name: detail.name, declared_resources: detail.declared_resources };
        }),
      );
      opDetails = details;
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      opsLoading = false;
    }
  }

  // ─────────────── Tab activation: lazy-load ───────────────
  let loaded = $state<Record<Tab, boolean>>({
    tokens: false,
    backends: false,
    extras: false,
    catalog: false,
    storage: false,
    config: false,
  });

  function activate(tab: Tab): void {
    activeTab = tab;
    if (loaded[tab]) return;
    loaded = { ...loaded, [tab]: true };
    switch (tab) {
      case 'tokens': void refreshTokens(); break;
      case 'backends': void refreshBackends(); break;
      case 'extras': void refreshExtras(); break;
      case 'catalog': void refreshCatalog(); break;
      case 'storage': void refreshStats(); break;
      case 'config': void refreshConfig(); break;
    }
  }

  onMount(() => activate('tokens'));

  const visibleCatalogOps = $derived.by(() => {
    if (!catalog) return [] as string[];
    const f = catalogFilter.toLowerCase();
    if (!f) return catalog.ops;
    return catalog.ops.filter((o) => o.toLowerCase().includes(f));
  });

  const visibleCatalogBackends = $derived.by(() => {
    if (!catalog) return [] as string[];
    const f = catalogFilter.toLowerCase();
    if (!f) return catalog.backends;
    return catalog.backends.filter((b) => b.toLowerCase().includes(f));
  });
</script>

<svelte:head>
  <title>media_engine · Settings</title>
</svelte:head>

<header class="mb-5">
  <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Settings</h1>
  <p class="text-sm" style="color: var(--text-secondary);">
    Tokens, backends, plugin catalog, storage, and effective config. Every tab maps directly to a
    <code class="font-mono text-xs">med</code> verb.
  </p>
</header>

<nav class="flex gap-1 mb-4 flex-wrap" aria-label="Settings tabs">
  {#each TABS as t (t.id)}
    <button
      type="button"
      onclick={() => activate(t.id)}
      class="px-3 py-1.5 rounded text-xs font-mono"
      style={activeTab === t.id
        ? 'background: var(--accent-green-soft); color: var(--accent-green); border: 1px solid var(--accent-green-line);'
        : 'background: var(--bg-card); color: var(--text-secondary); border: 1px solid var(--border-light);'}
      aria-current={activeTab === t.id ? 'page' : undefined}
    >
      {t.label}
    </button>
  {/each}
</nav>

{#if error}
  <p
    class="mb-3 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{error}</p>
{/if}

<!-- ─────────────── TOKENS ─────────────── -->
{#if activeTab === 'tokens'}
  <section class="p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    <h2 class="text-xs font-semibold uppercase mb-3" style="color: var(--text-muted);">Mint a new token</h2>
    <div class="flex gap-2 items-end mb-3">
      <label class="flex-1">
        <span class="block text-xs mb-1" style="color: var(--text-secondary);">Label</span>
        <input
          type="text"
          bind:value={newTokenLabel}
          placeholder="laptop"
          class="w-full px-2 py-1.5 rounded text-xs font-mono"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
        />
      </label>
      <label class="w-40">
        <span class="block text-xs mb-1" style="color: var(--text-secondary);">Namespace</span>
        <input
          type="text"
          bind:value={newTokenNamespace}
          class="w-full px-2 py-1.5 rounded text-xs font-mono"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
        />
      </label>
      <button
        type="button"
        onclick={() => void submitNewToken()}
        class="px-3 py-1.5 rounded text-xs font-semibold"
        style="background: var(--accent-green); color: var(--text-inverse);"
      >Mint</button>
    </div>

    {#if tokenError}
      <p class="text-xs mb-2" style="color: var(--accent-red);">{tokenError}</p>
    {/if}
    {#if newSecret}
      <div class="mb-3 p-3 rounded" style="background: var(--accent-amber-soft); border: 1px solid var(--accent-amber-line);">
        <div class="text-xs font-semibold mb-1" style="color: var(--accent-amber);">⚠ Copy this secret now — it won't be shown again:</div>
        <pre class="font-mono text-xs whitespace-pre-wrap break-all">{newSecret}</pre>
      </div>
    {/if}

    <h2 class="text-xs font-semibold uppercase mb-2 mt-4" style="color: var(--text-muted);">All tokens</h2>
    {#if tokensLoading}
      <p class="text-xs italic" style="color: var(--text-muted);">Loading…</p>
    {:else}
      <table class="w-full text-sm">
        <thead>
          <tr style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;">
            <th class="text-left px-2 py-1 font-semibold">id</th>
            <th class="text-left px-2 py-1 font-semibold">label</th>
            <th class="text-left px-2 py-1 font-semibold">namespace</th>
            <th class="text-left px-2 py-1 font-semibold">created</th>
            <th class="text-left px-2 py-1 font-semibold">revoked</th>
            <th class="text-right px-2 py-1 font-semibold">action</th>
          </tr>
        </thead>
        <tbody>
          {#each tokens as t (t.id)}
            {@const revoked = isRevoked(t)}
            <tr style="border-bottom: 1px solid var(--border-soft); opacity: {revoked ? 0.5 : 1};">
              <td class="px-2 py-1 text-xs font-mono">{t.id.slice(0, 12)}…</td>
              <td class="px-2 py-1 text-xs font-mono">{t.label || '—'}</td>
              <td class="px-2 py-1 text-xs font-mono" style="color: var(--text-secondary);">{t.namespace}</td>
              <td class="px-2 py-1 text-xs font-mono" style="color: var(--text-muted);">{new Date(t.created_at).toLocaleString()}</td>
              <td class="px-2 py-1 text-xs font-mono" style="color: var(--text-muted);">{t.revoked_at ? new Date(t.revoked_at).toLocaleString() : '—'}</td>
              <td class="px-2 py-1 text-right">
                {#if revoked}
                  <span class="text-xs font-mono" style="color: var(--text-muted);">revoked</span>
                {:else}
                  <button
                    type="button"
                    onclick={() => void revokeOne(t.id)}
                    class="px-2 py-0.5 rounded text-xs font-mono"
                    style="background: var(--bg-page); color: var(--accent-red); border: 1px solid rgba(220, 38, 38, 0.35);"
                  >revoke</button>
                {/if}
              </td>
            </tr>
          {/each}
          {#if tokens.length === 0}
            <tr><td colspan="6" class="px-2 py-4 text-center text-xs italic" style="color: var(--text-muted);">No tokens yet.</td></tr>
          {/if}
        </tbody>
      </table>
    {/if}
  </section>
{/if}

<!-- ─────────────── BACKENDS ─────────────── -->
{#if activeTab === 'backends'}
  <section class="p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    <div class="flex items-end justify-between mb-3">
      <h2 class="text-xs font-semibold uppercase" style="color: var(--text-muted);">Registered backends</h2>
      <button
        type="button"
        onclick={() => void refreshBackends()}
        disabled={backendsLoading}
        class="px-3 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
        style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      >{backendsLoading ? 'Refreshing…' : 'Refresh health'}</button>
    </div>

    {#if backendsLoading && backends.length === 0}
      <p class="text-xs italic" style="color: var(--text-muted);">Loading…</p>
    {:else}
      <table class="w-full text-sm">
        <thead>
          <tr style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;">
            <th class="text-left px-2 py-1 font-semibold">op</th>
            <th class="text-left px-2 py-1 font-semibold">backend</th>
            <th class="text-left px-2 py-1 font-semibold">version</th>
            <th class="text-right px-2 py-1 font-semibold">health</th>
          </tr>
        </thead>
        <tbody>
          {#each backends as b (`${b.op_name}__${b.name}`)}
            <tr style="border-bottom: 1px solid var(--border-soft);">
              <td class="px-2 py-1 text-xs font-mono">{b.op_name}</td>
              <td class="px-2 py-1 text-xs font-mono" style="color: var(--text-secondary);">{b.name}</td>
              <td class="px-2 py-1 text-xs font-mono" style="color: var(--text-muted);">{b.version}</td>
              <td class="px-2 py-1 text-right text-xs font-mono">
                {healthIcon(backendHealth[`${b.op_name}__${b.name}`])}
                {backendHealth[`${b.op_name}__${b.name}`] ?? 'unknown'}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </section>
{/if}

<!-- ─────────────── PLUGINS · EXTRAS ─────────────── -->
{#if activeTab === 'extras'}
  <section class="p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    <h2 class="text-xs font-semibold uppercase mb-1" style="color: var(--text-muted);">Optional extras</h2>
    <p class="text-xs mb-3" style="color: var(--text-secondary);">
      Run the command in your shell to install an extra. The Web UI never executes <code class="font-mono">uv sync</code>
      itself — auto-installing inside the live process risks corrupting the running venv.
      After installing, click <strong>Refresh</strong> to re-probe.
    </p>
    <button
      type="button"
      onclick={() => void refreshExtras()}
      disabled={extrasLoading}
      class="px-3 py-1.5 mb-3 rounded text-xs font-semibold disabled:opacity-50"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
    >{extrasLoading ? 'Refreshing…' : 'Refresh'}</button>

    <table class="w-full text-sm">
      <thead>
        <tr style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;">
          <th class="text-left px-2 py-1 font-semibold">extra</th>
          <th class="text-left px-2 py-1 font-semibold">packages</th>
          <th class="text-right px-2 py-1 font-semibold">installed</th>
          <th class="text-right px-2 py-1 font-semibold">command</th>
        </tr>
      </thead>
      <tbody>
        {#each extras as e (e.name)}
          <tr style="border-bottom: 1px solid var(--border-soft);">
            <td class="px-2 py-1 text-xs font-mono">{e.name}</td>
            <td class="px-2 py-1 text-xs font-mono" style="color: var(--text-muted);">{e.packages.join(', ')}</td>
            <td class="px-2 py-1 text-right text-xs font-mono">{e.installed ? '🟢 yes' : '⚪ no'}</td>
            <td class="px-2 py-1 text-right">
              <button
                type="button"
                onclick={() => void copyCommand(e.install_command)}
                class="px-2 py-0.5 rounded text-xs font-mono"
                style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
                title={e.install_command}
              >{copiedCommand === e.install_command ? '✓ copied' : 'copy'}</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </section>
{/if}

<!-- ─────────────── PLUGINS · CATALOG GATE ─────────────── -->
{#if activeTab === 'catalog'}
  <section class="p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    <h2 class="text-xs font-semibold uppercase mb-1" style="color: var(--text-muted);">Catalog gate</h2>
    <p class="text-xs mb-3" style="color: var(--text-secondary);">
      Hide ops or backends from discovery surfaces (REST <code class="font-mono">/operations</code>, MCP
      <code class="font-mono">tools/list</code>, the Web UI op picker). Hidden entries stay registered;
      this is enforcement-only filtering, not uninstall.
    </p>

    <div class="flex items-center gap-2 mb-3">
      <input
        type="search"
        bind:value={catalogFilter}
        placeholder="filter…"
        class="flex-1 px-2 py-1.5 rounded text-xs font-mono"
        style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      />
      <button
        type="button"
        onclick={() => void saveCatalog()}
        disabled={catalogSaving}
        class="px-3 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
        style="background: var(--accent-green); color: var(--text-inverse);"
      >{catalogSaving ? 'Saving…' : 'Save'}</button>
    </div>

    {#if catalogLoading && !catalog}
      <p class="text-xs italic" style="color: var(--text-muted);">Loading…</p>
    {:else if catalog}
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <h3 class="text-xs font-semibold mb-2" style="color: var(--text-secondary);">
            Ops ({visibleCatalogOps.length})
          </h3>
          <div class="max-h-[50vh] overflow-y-auto rounded" style="border: 1px solid var(--border-soft);">
            {#each visibleCatalogOps as op (op)}
              <label class="flex items-center gap-2 px-2 py-1 text-xs font-mono" style="border-bottom: 1px solid var(--border-soft);">
                <input
                  type="checkbox"
                  checked={hiddenOps.has(op)}
                  onchange={() => toggleOp(op)}
                />
                <span style="color: {hiddenOps.has(op) ? 'var(--text-muted)' : 'var(--text-primary)'};">
                  {op}
                </span>
              </label>
            {/each}
          </div>
        </div>

        <div>
          <h3 class="text-xs font-semibold mb-2" style="color: var(--text-secondary);">
            Backends ({visibleCatalogBackends.length})
          </h3>
          <div class="max-h-[50vh] overflow-y-auto rounded" style="border: 1px solid var(--border-soft);">
            {#each visibleCatalogBackends as bk (bk)}
              <label class="flex items-center gap-2 px-2 py-1 text-xs font-mono" style="border-bottom: 1px solid var(--border-soft);">
                <input
                  type="checkbox"
                  checked={hiddenBackends.has(bk)}
                  onchange={() => toggleBackend(bk)}
                />
                <span style="color: {hiddenBackends.has(bk) ? 'var(--text-muted)' : 'var(--text-primary)'};">
                  {bk}
                </span>
              </label>
            {/each}
          </div>
        </div>
      </div>
    {/if}
  </section>
{/if}

<!-- ─────────────── STORAGE ─────────────── -->
{#if activeTab === 'storage'}
  <section class="p-4 rounded mb-3" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    <div class="flex items-end justify-between mb-3">
      <h2 class="text-xs font-semibold uppercase" style="color: var(--text-muted);">Storage stats</h2>
      <button
        type="button"
        onclick={() => void refreshStats()}
        disabled={statsLoading}
        class="px-3 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
        style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      >{statsLoading ? 'Refreshing…' : 'Refresh'}</button>
    </div>

    {#if stats}
      <div class="grid grid-cols-3 gap-3 mb-4">
        <div class="rounded p-3" style="background: var(--bg-page); border: 1px solid var(--border-soft);">
          <div class="text-xs uppercase mb-1" style="color: var(--text-muted);">Total</div>
          <div class="font-mono text-sm">{formatBytes(stats.total_bytes)}</div>
        </div>
        <div class="rounded p-3" style="background: var(--bg-page); border: 1px solid var(--border-soft);">
          <div class="text-xs uppercase mb-1" style="color: var(--text-muted);">Free disk</div>
          <div class="font-mono text-sm">{stats.free_gb.toFixed(1)} GB</div>
        </div>
        <div class="rounded p-3" style="background: var(--bg-page); border: 1px solid var(--border-soft);">
          <div class="text-xs uppercase mb-1" style="color: var(--text-muted);">Namespace</div>
          <div class="font-mono text-sm">{stats.namespace}</div>
        </div>
      </div>

      <table class="w-full text-sm">
        <thead>
          <tr style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;">
            <th class="text-left px-2 py-1 font-semibold">kind</th>
            <th class="text-right px-2 py-1 font-semibold">count</th>
            <th class="text-right px-2 py-1 font-semibold">bytes</th>
          </tr>
        </thead>
        <tbody>
          {#each Object.entries(stats.by_kind).sort(([, a], [, b]) => b.bytes - a.bytes) as [kind, row] (kind)}
            <tr style="border-bottom: 1px solid var(--border-soft);">
              <td class="px-2 py-1 text-xs font-mono">{kind}</td>
              <td class="px-2 py-1 text-right text-xs font-mono">{row.count}</td>
              <td class="px-2 py-1 text-right text-xs font-mono">{formatBytes(row.bytes)}</td>
            </tr>
          {/each}
        </tbody>
      </table>

      <div class="mt-3 text-xs font-mono" style="color: var(--text-muted);">
        permanent: {stats.permanent_store}<br />
        workdir: {stats.workdir}
      </div>
    {/if}
  </section>

  <section class="p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    <h2 class="text-xs font-semibold uppercase mb-2" style="color: var(--text-muted);">Garbage collection</h2>
    <p class="text-xs mb-3" style="color: var(--text-secondary);">
      Sweep stale workdirs (default 24 h retention) + optional LRU eviction (off unless
      <code class="font-mono">eviction_enabled = true</code> in config).
    </p>
    <div class="flex gap-2 mb-3">
      <button
        type="button"
        onclick={() => void runGC(false)}
        disabled={gcRunning}
        class="px-3 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
        style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      >{gcRunning ? '…' : 'GC preview'}</button>
      <button
        type="button"
        onclick={() => void runGC(true)}
        disabled={gcRunning}
        class="px-3 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
        style="background: var(--accent-amber); color: var(--text-inverse);"
      >{gcRunning ? '…' : 'GC apply'}</button>
    </div>

    {#if gcResult}
      <div
        class="rounded p-3 text-xs font-mono"
        style="background: var(--bg-page); border: 1px solid var(--border-soft); color: var(--text-secondary);"
      >
        <div>Mode: {gcResult.applied ? 'applied' : 'preview'}</div>
        <div>Workdirs swept: {gcResult.workdirs_swept}</div>
        <div>Workdir candidates: {gcResult.workdir_candidates.length}</div>
        <div>Eviction enabled: {gcResult.eviction_enabled ? 'yes' : 'no'}</div>
        {#if gcResult.eviction_enabled}
          <div>Bytes before → after: {formatBytes(gcResult.bytes_before)} → {formatBytes(gcResult.bytes_after)}</div>
          <div>Evicted artifacts: {gcResult.evicted_artifact_ids.length}</div>
        {/if}
      </div>
    {/if}
  </section>
{/if}

<!-- ─────────────── CONFIG · read-only ─────────────── -->
{#if activeTab === 'config'}
  <section class="p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    <h2 class="text-xs font-semibold uppercase mb-1" style="color: var(--text-muted);">Effective resources</h2>
    <p class="text-xs mb-3" style="color: var(--text-secondary);">
      Resources declared by each registered op (the engine maps declared resource names to
      <code class="font-mono">asyncio.Semaphore</code> capacities at boot). Inline editor for
      <code class="font-mono">resources.yaml</code> lands in v1.x.
    </p>
    {#if opsLoading}
      <p class="text-xs italic" style="color: var(--text-muted);">Loading…</p>
    {:else}
      <table class="w-full text-sm">
        <thead>
          <tr style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;">
            <th class="text-left px-2 py-1 font-semibold">op</th>
            <th class="text-left px-2 py-1 font-semibold">declared resources</th>
          </tr>
        </thead>
        <tbody>
          {#each opDetails as op (op.name)}
            <tr style="border-bottom: 1px solid var(--border-soft);">
              <td class="px-2 py-1 text-xs font-mono">{op.name}</td>
              <td class="px-2 py-1 text-xs font-mono" style="color: var(--text-muted);">
                {op.declared_resources.length === 0 ? '—' : op.declared_resources.join(', ')}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
    <p class="mt-4 text-xs" style="color: var(--text-muted);">
      The full <code class="font-mono">EngineConfig</code> + <code class="font-mono">resources.yaml</code>
      content lives outside this UI today; run <code class="font-mono">med config</code> in your shell to
      see the merged values. Inline editors land in v1.x — <code class="font-mono">MEDIA_ENGINE_*</code> env vars
      are owned by the deploy.
    </p>
  </section>
{/if}
