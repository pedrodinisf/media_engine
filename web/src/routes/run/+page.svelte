<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { api, ApiError } from '$lib/api/client';
  import type { JobAck } from '$lib/api/client';
  import SchemaForm from '$lib/components/forms/SchemaForm.svelte';
  import { initialParams } from '$lib/components/forms/schema';
  import type { ParamsSchema, ParamsValue } from '$lib/components/forms/schema';

  type OperationSummary = {
    name: string;
    version: string;
    input_kinds: string[];
    output_kinds: string[];
    default_backend: string | null;
    variadic_inputs: boolean;
  };

  type OperationDetail = OperationSummary & {
    description: string;
    params_schema: ParamsSchema;
    declared_resources: string[];
    backends: string[];
  };

  type BackendDetail = {
    op_name: string;
    name: string;
    version: string;
    requires: Record<string, unknown>;
    health: string;
  };

  type RunPreview = {
    op: string;
    backend: string | null;
    estimate_seconds_local: number;
    estimate_cost_cents: number;
    estimate_tokens_in: number;
    estimate_tokens_out: number;
  };

  let ops = $state<OperationSummary[]>([]);
  let opsError: string | null = $state(null);

  let selectedOp = $state<string | null>(null);
  let opDetail = $state<OperationDetail | null>(null);
  let opDetailError: string | null = $state(null);

  let inputIdsText = $state('');
  let params = $state<ParamsValue>({});
  let backend = $state<string | null>(null);
  let backendHealth = $state<Record<string, string>>({});

  let preview = $state<RunPreview | null>(null);
  let previewError: string | null = $state(null);
  let previewBusy = $state(false);
  let submitting = $state(false);
  let submitError: string | null = $state(null);

  onMount(async () => {
    try {
      ops = await api.get<OperationSummary[]>('/operations');
    } catch (e) {
      opsError = e instanceof ApiError ? e.detail : String(e);
    }
  });

  async function selectOp(name: string): Promise<void> {
    selectedOp = name;
    opDetail = null;
    opDetailError = null;
    preview = null;
    previewError = null;
    backendHealth = {};
    // Stale input ids from the previous op would 422 against a new op
    // expecting different kinds — clear them when the user switches.
    inputIdsText = '';
    try {
      opDetail = await api.get<OperationDetail>(`/operations/${name}`);
      params = initialParams(opDetail.params_schema);
      backend = opDetail.default_backend;
      // Health probe for every backend (best-effort; failed probes
      // render as a question mark in the picker).
      await Promise.all(
        opDetail.backends.map(async (b) => {
          try {
            const detail = await api.get<BackendDetail>(
              `/backends/${b}?op=${encodeURIComponent(name)}`,
            );
            backendHealth = { ...backendHealth, [b]: detail.health };
          } catch {
            backendHealth = { ...backendHealth, [b]: 'unknown' };
          }
        }),
      );
    } catch (e) {
      opDetailError = e instanceof ApiError ? e.detail : String(e);
    }
  }

  function inputIds(): string[] {
    return inputIdsText
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  // Debounced cost preview — refresh whenever the op, backend, params,
  // or inputs change. Returning a cleanup from $effect cancels both
  // the pending timer AND ignores the in-flight fetch result on unmount
  // (or before the next effect run fires) so we never write to dead
  // state.
  $effect(() => {
    if (!selectedOp || !opDetail) return;
    // Capture deps so the effect re-runs on change.
    const _op = selectedOp;
    const _backend = backend;
    const _params = params;
    const _inputs = inputIds();
    let cancelled = false;
    const timer = setTimeout(async () => {
      previewBusy = true;
      previewError = null;
      try {
        const body: { op: string; inputs: string[]; backend?: string; params: ParamsValue } = {
          op: _op,
          inputs: _inputs,
          params: _params,
        };
        if (_backend) body.backend = _backend;
        const result = await api.post<RunPreview>('/run/preview', body);
        if (!cancelled) preview = result;
      } catch (e) {
        if (!cancelled) {
          preview = null;
          previewError = e instanceof ApiError ? e.detail : String(e);
        }
      } finally {
        if (!cancelled) previewBusy = false;
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  });

  async function submit(): Promise<void> {
    if (!selectedOp) return;
    submitting = true;
    submitError = null;
    try {
      const body: { op: string; inputs: string[]; backend?: string; params: ParamsValue } = {
        op: selectedOp,
        inputs: inputIds(),
        params,
      };
      if (backend) body.backend = backend;
      const ack = await api.post<JobAck>('/run', body);
      await goto(`${base}/jobs/${ack.job_id}`);
    } catch (e) {
      submitError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      submitting = false;
    }
  }

  function healthIcon(state: string | undefined): string {
    switch (state) {
      case 'ok':
        return '🟢';
      case 'degraded':
        return '🟡';
      case 'unavailable':
        return '🔴';
      default:
        return '⚪';
    }
  }
</script>

<svelte:head>
  <title>media_engine · Run</title>
</svelte:head>

<header class="mb-6">
  <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Run an op</h1>
  <p class="text-sm" style="color: var(--text-secondary);">
    Pick an op, point it at input artifact ids, fill the schema-driven form.
    The cost preview updates live; submit hands off to <code class="font-mono text-xs">POST /run</code>.
  </p>
</header>

<div class="grid grid-cols-12 gap-4">
  <section class="col-span-4 p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    <h2 class="text-xs font-semibold uppercase mb-3" style="color: var(--text-muted);">Operations</h2>
    {#if opsError}
      <p class="text-xs" style="color: var(--accent-red);">{opsError}</p>
    {/if}
    <ul class="text-sm max-h-[60vh] overflow-y-auto pr-1">
      {#each ops as op (op.name)}
        <li>
          <button
            type="button"
            onclick={() => selectOp(op.name)}
            class="w-full text-left px-2 py-1.5 rounded font-mono text-xs"
            style={selectedOp === op.name
              ? 'background: var(--accent-green-soft); color: var(--text-primary); border-left: 3px solid var(--accent-green); padding-left: 0.5rem;'
              : 'color: var(--text-secondary);'}
          >
            {op.name}
          </button>
        </li>
      {/each}
    </ul>
  </section>

  <section class="col-span-8 p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
    {#if !selectedOp}
      <p class="text-sm italic" style="color: var(--text-muted);">Select an op on the left to configure a run.</p>
    {:else if opDetailError}
      <p class="text-xs" style="color: var(--accent-red);">{opDetailError}</p>
    {:else if opDetail}
      <header class="mb-4 pb-3" style="border-bottom: 1px solid var(--border-soft);">
        <h2 class="text-base font-semibold font-mono" style="color: var(--text-primary);">{opDetail.name}</h2>
        <p class="text-xs mt-1" style="color: var(--text-muted);">
          v{opDetail.version} · {opDetail.input_kinds.length === 0 ? 'no inputs' : `${opDetail.input_kinds.join(' | ')} → ${opDetail.output_kinds.join(' | ')}`}
        </p>
        {#if opDetail.description}
          <p class="text-xs mt-2 whitespace-pre-wrap" style="color: var(--text-secondary);">
            {opDetail.description.split('\n')[0]}
          </p>
        {/if}
      </header>

      {#if opDetail.input_kinds.length > 0}
        <label class="block mb-3">
          <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">
            Input artifact ids
          </span>
          <span class="block text-xs mb-1.5" style="color: var(--text-muted);">
            Space- or comma-separated. Order matters when the op declares multiple input kinds.
          </span>
          <input
            type="text"
            bind:value={inputIdsText}
            class="w-full px-3 py-2 rounded text-sm font-mono"
            style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
            placeholder="e.g. a-3c1f… b-9b2c…"
          />
        </label>
      {/if}

      {#if opDetail.backends.length > 0}
        <label class="block mb-3">
          <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">Backend</span>
          <select
            bind:value={backend}
            class="px-3 py-2 rounded text-sm font-mono"
            style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
          >
            <option value={null}>—</option>
            {#each opDetail.backends as b (b)}
              <option value={b}>{healthIcon(backendHealth[b])} {b}</option>
            {/each}
          </select>
          {#if opDetail.default_backend}
            <span class="ml-3 text-xs" style="color: var(--text-muted);">
              default: <code class="font-mono">{opDetail.default_backend}</code>
            </span>
          {/if}
        </label>
      {/if}

      <SchemaForm
        schema={opDetail.params_schema}
        value={params}
        onChange={(next) => (params = next)}
      />

      <div class="mt-5 pt-4 flex items-end justify-between gap-4" style="border-top: 1px solid var(--border-soft);">
        <div class="text-xs font-mono" style="color: var(--text-secondary);">
          {#if previewBusy}
            <span style="color: var(--text-muted);">Estimating…</span>
          {:else if preview}
            <div>
              <span style="color: var(--text-muted);">backend:</span> {preview.backend ?? '—'}
            </div>
            <div>
              <span style="color: var(--text-muted);">cost:</span>
              {#if preview.estimate_cost_cents > 0}
                {(preview.estimate_cost_cents / 100).toFixed(4)} USD
              {:else if preview.estimate_seconds_local > 0}
                ~{preview.estimate_seconds_local.toFixed(1)} s local
              {:else}
                free / cached
              {/if}
            </div>
            {#if preview.estimate_tokens_in > 0 || preview.estimate_tokens_out > 0}
              <div>
                <span style="color: var(--text-muted);">tokens:</span>
                {preview.estimate_tokens_in} in · {preview.estimate_tokens_out} out
              </div>
            {/if}
          {:else if previewError}
            <span style="color: var(--accent-red);">{previewError}</span>
          {/if}
        </div>

        <button
          type="button"
          disabled={!selectedOp || submitting}
          onclick={submit}
          class="px-5 py-2 rounded text-sm font-semibold disabled:opacity-50"
          style="background: var(--accent-green); color: var(--text-inverse);"
        >
          {submitting ? 'Submitting…' : 'Run'}
        </button>
      </div>

      {#if submitError}
        <p
          class="mt-3 text-xs p-2 rounded"
          style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
        >{submitError}</p>
      {/if}
    {/if}
  </section>
</div>
