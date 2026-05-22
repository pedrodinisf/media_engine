<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/stores';
  import { api, ApiError } from '$lib/api/client';
  import { openSSE, type SSEEvent } from '$lib/sse/event-source';

  type JobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';

  type Job = {
    id: string;
    status: JobStatus;
    pipeline_name: string | null;
    namespace: string;
    started_at: string | null;
    finished_at: string | null;
    op_run_ids: string[];
    failure_envelope?: unknown;
  };

  type OperationRunRef = {
    id: string;
    op_name: string;
    backend_name: string | null;
    output_artifact_ids: string[];
  };

  type JobDetail = {
    job: Job;
    op_runs: OperationRunRef[];
  };

  let detail: JobDetail | null = $state(null);
  let error: string | null = $state(null);
  let activeTab: 'events' | 'op_runs' | 'outputs' | 'failure' = $state('events');
  let events: SSEEvent[] = $state([]);
  const jobId = $derived($page.params.id ?? '');

  async function refresh(currentJobId: string): Promise<void> {
    try {
      detail = await api.get<JobDetail>(`/jobs/${currentJobId}`);
      error = null;
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    }
  }

  function appendEvent(ev: SSEEvent): void {
    events = [...events, ev].slice(-500);
  }

  // SvelteKit reuses this component across same-route navigations
  // (`/jobs/abc → /jobs/def`), so onMount alone would leave the SSE
  // stream + refresh pointed at the OLD jobId. `$effect` re-runs on
  // every jobId change and returns a cleanup that closes the prior
  // stream + clears the event tail before reconnecting.
  $effect(() => {
    if (!jobId) return;
    events = [];
    detail = null;
    error = null;
    void refresh(jobId);
    const close = openSSE(`/jobs/${jobId}/events`, {
      onEvent: (ev) => {
        appendEvent(ev);
        // Status transitions arrive as OpStarted/OpCompleted/OpFailed;
        // refresh the job row so the badge updates.
        if (
          ev.type === 'OpStarted' ||
          ev.type === 'OpCompleted' ||
          ev.type === 'OpFailed'
        ) {
          void refresh(jobId);
        }
      },
    });
    return () => close();
  });

  function formatEvent(ev: SSEEvent): string {
    try {
      const data = JSON.parse(ev.data);
      if (data.message) return `${ev.type}: ${data.message}`;
      if (data.fraction !== undefined) {
        return `${ev.type}: ${(data.fraction * 100).toFixed(0)}%${data.label ? ' ' + data.label : ''}`;
      }
      if (data.op_run_id) return `${ev.type}: ${data.op_run_id}`;
      return ev.type;
    } catch {
      return ev.type;
    }
  }
</script>

<svelte:head>
  <title>media_engine · job {jobId.slice(0, 8)}</title>
</svelte:head>

<header class="mb-5">
  <p class="text-xs font-mono mb-1" style="color: var(--text-muted);">
    <a href="{base}/jobs">jobs</a> / {jobId}
  </p>
  {#if detail}
    <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">
      {detail.job.pipeline_name ?? 'Run'}
    </h1>
    <div class="flex gap-3 text-xs items-center">
      <span
        class="font-mono px-2 py-0.5 rounded"
        style="border: 1px solid var(--border-light); background: var(--bg-card); color: var(--text-primary);"
      >
        {detail.job.status}
      </span>
      <span style="color: var(--text-muted);">
        ns: <code class="font-mono">{detail.job.namespace}</code>
      </span>
      {#if detail.job.started_at}
        <span style="color: var(--text-muted);">
          started {new Date(detail.job.started_at).toLocaleTimeString()}
        </span>
      {/if}
      {#if detail.job.finished_at}
        <span style="color: var(--text-muted);">
          finished {new Date(detail.job.finished_at).toLocaleTimeString()}
        </span>
      {/if}
    </div>
  {/if}
</header>

{#if error}
  <p
    class="mb-4 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{error}</p>
{/if}

<div class="flex gap-1 mb-4 text-sm">
  {#each [
    { id: 'events', label: 'Events' },
    { id: 'op_runs', label: 'Op runs' },
    { id: 'outputs', label: 'Outputs' },
    { id: 'failure', label: 'Failure' },
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

<section class="p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
  {#if activeTab === 'events'}
    {#if events.length === 0}
      <p class="text-xs italic" style="color: var(--text-muted);">
        Waiting for events…
      </p>
    {:else}
      <ul class="font-mono text-xs space-y-0.5 max-h-[60vh] overflow-y-auto">
        {#each events as ev, i (i)}
          <li style="color: var(--text-secondary);">{formatEvent(ev)}</li>
        {/each}
      </ul>
    {/if}
  {:else if activeTab === 'op_runs'}
    {#if detail?.op_runs && detail.op_runs.length > 0}
      <ul class="font-mono text-xs space-y-1">
        {#each detail.op_runs as run (run.id)}
          <li>
            <code style="color: var(--text-primary);">{run.op_name}</code>
            <span style="color: var(--text-muted);">
              {run.backend_name ? `· ${run.backend_name}` : ''}
              · {run.output_artifact_ids.length} output{run.output_artifact_ids.length === 1 ? '' : 's'}
            </span>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="text-xs italic" style="color: var(--text-muted);">No op runs recorded yet.</p>
    {/if}
  {:else if activeTab === 'outputs'}
    {#if detail}
      {@const outIds = detail.op_runs.flatMap((r) => r.output_artifact_ids)}
      {#if outIds.length === 0}
        <p class="text-xs italic" style="color: var(--text-muted);">No output artifacts yet.</p>
      {:else}
        <ul class="font-mono text-xs space-y-1">
          {#each outIds as artId (artId)}
            <li>
              <a href="{base}/catalog/{artId}" style="color: var(--accent-green);">{artId.slice(0, 16)}…</a>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  {:else if activeTab === 'failure'}
    {#if detail?.job.failure_envelope}
      <pre
        class="font-mono text-xs whitespace-pre-wrap p-3 rounded"
        style="background: var(--bg-deep); border: 1px solid var(--border-warm); color: var(--text-primary);"
      >{JSON.stringify(detail.job.failure_envelope, null, 2)}</pre>
    {:else}
      <p class="text-xs italic" style="color: var(--text-muted);">No failure recorded.</p>
    {/if}
  {/if}
</section>
