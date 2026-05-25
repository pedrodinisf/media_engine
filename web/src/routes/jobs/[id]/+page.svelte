<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/stores';
  import { api, ApiError } from '$lib/api/client';
  import { openSSE, type SSEEvent } from '$lib/sse/event-source';

  type JobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';

  /**
   * Mirror of media_engine/api/jobs.py::_classify_error — the dict
   * persisted into Job.error when a submission/op raises. Older code
   * referenced this as `failure_envelope` and never matched the server
   * field name (B-011 p0): every failed job rendered "No failure
   * recorded" even when the error was there.
   */
  type JobError = {
    error_class: string;
    message: string;
    retryable: boolean;
    suggested_action: string | null;
    traceback: string | null;
  };

  type Job = {
    id: string;
    status: JobStatus;
    pipeline_name: string | null;
    namespace: string;
    started_at: string | null;
    finished_at: string | null;
    op_run_ids: string[];
    error?: JobError | null;
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

  /**
   * Heartbeat Progress shape — three optional fields the engine's
   * runtime/heartbeat task populates on every tick. Mirrors
   * `media_engine.runtime.events.Progress` since Phase A.1.
   */
  type ProgressData = {
    fraction?: number;
    message?: string;
    phase?: string | null;
    available_memory_gb?: number | null;
    eta_seconds?: number | null;
    pool_bytes_estimate?: number | null;
  };

  type LogLineData = {
    level: string;
    source: string;
    line: string;
  };

  let detail: JobDetail | null = $state(null);
  let error: string | null = $state(null);
  let activeTab: 'events' | 'logs' | 'op_runs' | 'outputs' | 'failure' = $state('events');
  let events: SSEEvent[] = $state([]);
  let showTraceback = $state(false);
  // Latest heartbeat-phase Progress snapshot for the status-header gauges.
  let lastHeartbeat: ProgressData | null = $state(null);
  // Per-source filter for the Logs tab; '' = all sources.
  let logSourceFilter = $state('');
  const jobId = $derived($page.params.id ?? '');

  // Distinct log sources observed in this run, for the filter dropdown.
  const logSources = $derived.by(() => {
    const seen = new Set<string>();
    for (const ev of events) {
      if (ev.type !== 'log_line') continue;
      try {
        const data = JSON.parse(ev.data) as LogLineData;
        if (data.source) seen.add(data.source);
      } catch {
        // ignore
      }
    }
    return Array.from(seen).sort();
  });

  const filteredLogs = $derived.by(() => {
    const out: Array<{ source: string; level: string; line: string }> = [];
    for (const ev of events) {
      if (ev.type !== 'log_line') continue;
      try {
        const data = JSON.parse(ev.data) as LogLineData;
        if (logSourceFilter && data.source !== logSourceFilter) continue;
        out.push({ source: data.source, level: data.level, line: data.line });
      } catch {
        // ignore malformed frames
      }
    }
    return out;
  });

  function formatRamGb(gb: number | null | undefined): string {
    if (gb == null) return '—';
    return `${gb.toFixed(1)} GB`;
  }

  function formatEta(seconds: number | null | undefined): string {
    if (seconds == null) return '—';
    if (seconds < 1) return '<1s';
    if (seconds < 60) return `${seconds.toFixed(0)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}m${s.toString().padStart(2, '0')}s`;
  }

  // A job is in a terminal state if no further events will arrive. Used
  // by the Events tab to swap the indefinite "Waiting for events…"
  // placeholder for a one-shot "no events were recorded" message when
  // the engine bailed before emitting op_started (B-012 p1) — e.g. an
  // input-kind validation failure rejects the submission before any
  // op_run row is created.
  const isTerminal = $derived.by(() => {
    const s = detail?.job.status;
    return s === 'failed' || s === 'completed' || s === 'cancelled';
  });

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
    // Pluck heartbeat Progress events into the status-bar gauges so the
    // UI doesn't have to scan the full event tail on every render.
    if (ev.type === 'progress') {
      try {
        const data = JSON.parse(ev.data) as ProgressData;
        if (data.phase === 'heartbeat') {
          lastHeartbeat = data;
        }
      } catch {
        // ignore malformed frames
      }
    }
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
    lastHeartbeat = null;
    void refresh(jobId);
    const close = openSSE(`/jobs/${jobId}/events`, {
      onEvent: (ev) => {
        appendEvent(ev);
        // Status transitions arrive as op_started / op_completed /
        // op_failed (snake_case matching the server-side Event.type
        // literals). PascalCase here was the B-001 client-side bug.
        if (
          ev.type === 'op_started' ||
          ev.type === 'op_completed' ||
          ev.type === 'op_failed'
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
      <!--
        Live RAM-free + ETA gauges sourced from the engine's per-run
        heartbeat task (`media_engine.runtime.heartbeat`). Hidden once
        the job hits a terminal state so a stale snapshot doesn't
        misrepresent a finished run.
      -->
      {#if lastHeartbeat && !isTerminal}
        <span
          class="font-mono px-2 py-0.5 rounded"
          style="background: var(--bg-page); color: var(--text-secondary); border: 1px solid var(--border-light);"
          title="Host RAM available right now (heartbeat-sampled)"
          data-test="job-ram-gauge"
        >
          RAM {formatRamGb(lastHeartbeat.available_memory_gb)}
        </span>
        <span
          class="font-mono px-2 py-0.5 rounded"
          style="background: var(--bg-page); color: var(--text-secondary); border: 1px solid var(--border-light);"
          title="Estimated time remaining (from op.cost_estimate)"
          data-test="job-eta-gauge"
        >
          ETA {formatEta(lastHeartbeat.eta_seconds)}
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
    { id: 'logs', label: 'Logs' },
    { id: 'op_runs', label: 'Op runs' },
    { id: 'outputs', label: 'Outputs' },
    { id: 'failure', label: 'Failure' },
  ] as tab (tab.id)}
    <button
      type="button"
      onclick={() => (activeTab = tab.id as typeof activeTab)}
      class="px-3 py-1.5 rounded text-xs font-mono transition-colors hover:brightness-95 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-green"
      style={activeTab === tab.id
        ? 'background: var(--accent-green-soft); color: var(--accent-green); border: 1.5px solid var(--accent-green); font-weight: 600;'
        : 'background: var(--bg-alt); color: var(--text-primary); border: 1px solid var(--border-warm);'}
      aria-current={activeTab === tab.id ? 'page' : undefined}
    >
      {tab.label}
    </button>
  {/each}
</div>

<section class="p-4 rounded" style="background: var(--bg-card); border: 1px solid var(--border-soft);">
  {#if activeTab === 'events'}
    {#if events.length === 0 && isTerminal}
      <p class="text-xs italic" style="color: var(--text-muted);">
        No events were recorded for this job.
        {#if detail?.job.status === 'failed'}
          The engine rejected the submission before any op started —
          check the <button
            type="button"
            class="underline"
            onclick={() => (activeTab = 'failure')}
          >Failure tab</button> for the reason.
        {/if}
      </p>
    {:else if events.length === 0}
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
  {:else if activeTab === 'logs'}
    <div class="space-y-2" data-test="job-logs-pane">
      <div class="flex items-center gap-2 text-xs">
        <label for="log-source-filter" style="color: var(--text-muted);">Source</label>
        <select
          id="log-source-filter"
          class="px-2 py-1 rounded font-mono text-xs"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
          bind:value={logSourceFilter}
          data-test="job-logs-source-filter"
        >
          <option value="">all</option>
          {#each logSources as src (src)}
            <option value={src}>{src}</option>
          {/each}
        </select>
        <span style="color: var(--text-muted);">
          {filteredLogs.length} line{filteredLogs.length === 1 ? '' : 's'}
        </span>
      </div>
      {#if filteredLogs.length === 0}
        <p class="text-xs italic" style="color: var(--text-muted);">
          {logSourceFilter
            ? `No log lines from "${logSourceFilter}" yet.`
            : isTerminal
              ? 'No log lines were captured for this run.'
              : 'Waiting for log output…'}
        </p>
      {:else}
        <ul
          class="font-mono text-xs leading-snug max-h-[60vh] overflow-y-auto p-2 rounded"
          style="background: var(--bg-deep); border: 1px solid var(--border-warm);"
        >
          {#each filteredLogs as entry, i (i)}
            <li
              style="color: {entry.level === 'warn' || entry.level === 'warning'
                ? 'var(--accent-amber)'
                : entry.level === 'error' || entry.level === 'critical'
                  ? 'var(--accent-red)'
                  : 'var(--text-secondary)'};"
            >
              <span style="color: var(--text-muted);">[{entry.source}]</span>
              {entry.line}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
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
    {#if detail?.job.error}
      {@const err = detail.job.error}
      <div class="space-y-3">
        <div class="flex items-start gap-2 flex-wrap">
          <span
            class="font-mono text-xs px-2 py-0.5 rounded"
            style="background: var(--accent-red-soft); color: var(--accent-red); border: 1px solid rgba(220, 38, 38, 0.35);"
          >{err.error_class}</span>
          <span
            class="font-mono text-xs px-2 py-0.5 rounded"
            style="background: var(--bg-page); color: var(--text-muted); border: 1px solid var(--border-light);"
          >{err.retryable ? 'retryable' : 'not retryable'}</span>
        </div>
        <p class="text-sm font-mono whitespace-pre-wrap" style="color: var(--text-primary);">
          {err.message}
        </p>
        {#if err.suggested_action}
          <p
            class="text-xs p-3 rounded"
            style="background: var(--accent-amber-soft); color: var(--accent-amber); border: 1px solid var(--accent-amber-line);"
          >
            <strong>Suggestion:</strong> {err.suggested_action}
          </p>
        {/if}
        {#if err.traceback}
          <details bind:open={showTraceback}>
            <summary class="text-xs cursor-pointer" style="color: var(--text-secondary);">
              {showTraceback ? 'Hide' : 'Show'} traceback
            </summary>
            <pre
              class="font-mono text-xs whitespace-pre-wrap p-3 rounded mt-2 max-h-[50vh] overflow-y-auto"
              style="background: var(--bg-deep); border: 1px solid var(--border-warm); color: var(--text-secondary);"
            >{err.traceback}</pre>
          </details>
        {/if}
      </div>
    {:else}
      <p class="text-xs italic" style="color: var(--text-muted);">No failure recorded.</p>
    {/if}
  {/if}
</section>
