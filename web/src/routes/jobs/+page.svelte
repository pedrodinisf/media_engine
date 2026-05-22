<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { api, ApiError } from '$lib/api/client';
  import { openSSE } from '$lib/sse/event-source';

  type JobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';

  type Job = {
    id: string;
    status: JobStatus;
    pipeline_name: string | null;
    namespace: string;
    started_at: string | null;
    finished_at: string | null;
    op_run_ids: string[];
  };

  let jobs: Job[] = $state([]);
  let error: string | null = $state(null);
  let statusFilter: JobStatus | 'all' = $state('all');
  let cancelling: Set<string> = $state(new Set());
  let closeStream: (() => void) | null = null;

  async function refresh(): Promise<void> {
    try {
      const path =
        statusFilter === 'all'
          ? '/jobs?limit=100'
          : `/jobs?limit=100&status=${statusFilter}`;
      jobs = await api.get<Job[]>(path);
      error = null;
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    }
  }

  async function cancelJob(jobId: string): Promise<void> {
    cancelling = new Set([...cancelling, jobId]);
    try {
      await api.delete(`/jobs/${jobId}`);
      await refresh();
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
    } finally {
      cancelling = new Set([...cancelling].filter((id) => id !== jobId));
    }
  }

  function statusStyle(s: JobStatus): string {
    switch (s) {
      case 'pending':
        return 'background: var(--bg-alt); color: var(--text-muted);';
      case 'running':
        return 'background: var(--accent-green-soft); color: var(--accent-green); border-color: var(--accent-green-line);';
      case 'completed':
        return 'background: var(--accent-green-soft); color: var(--accent-green); border-color: var(--accent-green-line);';
      case 'failed':
        return 'background: var(--accent-red-soft); color: var(--accent-red); border-color: rgba(220,38,38,0.25);';
      case 'cancelled':
        return 'background: var(--accent-amber-soft); color: var(--accent-amber); border-color: var(--accent-amber-line);';
    }
  }

  onMount(() => {
    void refresh();
    // SSE-driven live refresh on any global event. Cheap enough at
    // local scale — for big deployments we'd debounce the refetch.
    closeStream = openSSE('/events/stream', {
      onEvent: () => void refresh(),
    });
    // Also poll as a backstop (cache hits don't emit events; status
    // transitions tied to in-flight task completion arrive via the
    // bus, but periodic refetch picks up rows missed during a brief
    // disconnect).
    const interval = setInterval(() => void refresh(), 5000);
    return () => clearInterval(interval);
  });

  onDestroy(() => {
    closeStream?.();
  });
</script>

<svelte:head>
  <title>media_engine · Jobs</title>
</svelte:head>

<header class="mb-5 flex items-end justify-between">
  <div>
    <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Jobs</h1>
    <p class="text-sm" style="color: var(--text-secondary);">
      Live status across every async submission. Auto-refreshes via SSE.
    </p>
  </div>
  <label class="flex items-center gap-2 text-xs" style="color: var(--text-secondary);">
    Status
    <select
      bind:value={statusFilter}
      onchange={() => void refresh()}
      class="px-2 py-1 rounded text-xs font-mono"
      style="background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-light);"
    >
      <option value="all">all</option>
      <option value="pending">pending</option>
      <option value="running">running</option>
      <option value="completed">completed</option>
      <option value="failed">failed</option>
      <option value="cancelled">cancelled</option>
    </select>
  </label>
</header>

{#if error}
  <p
    class="mb-4 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{error}</p>
{/if}

<table class="w-full text-sm" style="background: var(--bg-card); border: 1px solid var(--border-soft); border-radius: 4px;">
  <thead>
    <tr style="border-bottom: 1px solid var(--border-soft); color: var(--text-muted); font-size: 11px; text-transform: uppercase;">
      <th class="text-left px-3 py-2 font-semibold">id</th>
      <th class="text-left px-3 py-2 font-semibold">status</th>
      <th class="text-left px-3 py-2 font-semibold">pipeline</th>
      <th class="text-left px-3 py-2 font-semibold">started</th>
      <th class="text-left px-3 py-2 font-semibold">op runs</th>
      <th class="text-right px-3 py-2 font-semibold"></th>
    </tr>
  </thead>
  <tbody>
    {#each jobs as job (job.id)}
      <tr style="border-bottom: 1px solid var(--border-soft);">
        <td class="px-3 py-2 font-mono text-xs">
          <a href="/jobs/{job.id}" style="color: var(--text-primary);">
            {job.id.slice(0, 12)}…
          </a>
        </td>
        <td class="px-3 py-2">
          <span
            class="text-xs font-mono px-2 py-0.5 rounded"
            style="border: 1px solid; {statusStyle(job.status)}"
          >
            {job.status}
          </span>
        </td>
        <td class="px-3 py-2 text-xs" style="color: var(--text-secondary);">
          {job.pipeline_name ?? '—'}
        </td>
        <td class="px-3 py-2 text-xs font-mono" style="color: var(--text-muted);">
          {job.started_at ? new Date(job.started_at).toLocaleTimeString() : '—'}
        </td>
        <td class="px-3 py-2 text-xs font-mono" style="color: var(--text-muted);">
          {job.op_run_ids.length}
        </td>
        <td class="px-3 py-2 text-right">
          {#if job.status === 'running' || job.status === 'pending'}
            <button
              type="button"
              disabled={cancelling.has(job.id)}
              onclick={() => void cancelJob(job.id)}
              class="text-xs px-2 py-1 rounded font-medium disabled:opacity-50"
              style="color: var(--accent-red); border: 1px solid rgba(220,38,38,0.25);"
            >
              {cancelling.has(job.id) ? '…' : 'cancel'}
            </button>
          {/if}
        </td>
      </tr>
    {/each}
    {#if jobs.length === 0 && !error}
      <tr>
        <td colspan="6" class="px-3 py-6 text-center text-xs italic" style="color: var(--text-muted);">
          No jobs yet. Submit one via <a href="/ingest">Ingest</a> or <a href="/run">Run</a>.
        </td>
      </tr>
    {/if}
  </tbody>
</table>
