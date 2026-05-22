<script lang="ts">
  import { goto } from '$app/navigation';
  import { api, ApiError } from '$lib/api/client';
  import type { JobAck, UploadPreview, URLProbeResponse } from '$lib/api/client';

  type Tab = 'upload' | 'url' | 'livestream' | 'batch';
  let activeTab: Tab = $state('upload');

  // ─────────── Upload tab ───────────
  let uploadFile: File | null = $state(null);
  let uploadPreview: UploadPreview | null = $state(null);
  let uploadStatus: string | null = $state(null);
  let uploadError: string | null = $state(null);
  let uploadBusy = $state(false);

  function pickFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    uploadFile = input.files?.[0] ?? null;
    uploadPreview = null;
    uploadError = null;
    uploadStatus = null;
  }

  function handleDrop(event: DragEvent): void {
    event.preventDefault();
    const f = event.dataTransfer?.files?.[0];
    if (f) {
      uploadFile = f;
      uploadPreview = null;
      uploadError = null;
      uploadStatus = null;
    }
  }

  async function previewUpload(): Promise<void> {
    if (!uploadFile) return;
    uploadBusy = true;
    uploadError = null;
    uploadStatus = 'Probing…';
    try {
      const fd = new FormData();
      fd.append('file', uploadFile);
      fd.append('commit', 'false');
      uploadPreview = await api.postForm<UploadPreview>('/acquire/upload', fd);
      uploadStatus = 'Ready to commit.';
    } catch (e) {
      uploadError = e instanceof ApiError ? e.detail : String(e);
      uploadStatus = null;
    } finally {
      uploadBusy = false;
    }
  }

  async function commitUpload(): Promise<void> {
    if (!uploadFile) return;
    uploadBusy = true;
    uploadError = null;
    uploadStatus = 'Uploading…';
    try {
      const fd = new FormData();
      fd.append('file', uploadFile);
      fd.append('commit', 'true');
      const ack = await api.postForm<JobAck>('/acquire/upload', fd);
      await goto(`/jobs/${ack.job_id}`);
    } catch (e) {
      uploadError = e instanceof ApiError ? e.detail : String(e);
      uploadStatus = null;
    } finally {
      uploadBusy = false;
    }
  }

  // ─────────── URL tab ───────────
  let urlInput = $state('');
  let urlQuality = $state('best');
  let urlProbe: URLProbeResponse | null = $state(null);
  let urlError: string | null = $state(null);
  let urlBusy = $state(false);

  async function probeUrl(): Promise<void> {
    urlBusy = true;
    urlError = null;
    urlProbe = null;
    try {
      urlProbe = await api.post<URLProbeResponse>('/acquire/url/probe', { url: urlInput });
      if (!urlProbe.resolvable) {
        urlError = urlProbe.reason ?? 'URL did not resolve.';
      }
    } catch (e) {
      urlError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      urlBusy = false;
    }
  }

  async function commitUrl(): Promise<void> {
    urlBusy = true;
    urlError = null;
    try {
      const ack = await api.post<JobAck>('/run', {
        op: 'acquire.url',
        inputs: [],
        params: { url: urlInput, quality: urlQuality },
      });
      await goto(`/jobs/${ack.job_id}`);
    } catch (e) {
      urlError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      urlBusy = false;
    }
  }

  // ─────────── Livestream tab ───────────
  let liveUrl = $state('');
  let liveMaxDuration = $state(3600);
  let liveSegmentSeconds: number | null = $state(null);
  let liveError: string | null = $state(null);
  let liveBusy = $state(false);

  async function commitLive(): Promise<void> {
    liveBusy = true;
    liveError = null;
    try {
      type LiveParams = {
        url: string;
        max_duration: number;
        segment_seconds?: number;
      };
      const params: LiveParams = {
        url: liveUrl,
        max_duration: liveMaxDuration,
      };
      if (liveSegmentSeconds !== null) {
        params.segment_seconds = liveSegmentSeconds;
      }
      const ack = await api.post<JobAck>('/run', {
        op: 'acquire.livestream',
        inputs: [],
        params,
      });
      await goto(`/jobs/${ack.job_id}`);
    } catch (e) {
      liveError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      liveBusy = false;
    }
  }

  // ─────────── Batch tab ───────────
  let batchText = $state('');
  let batchQuality = $state('best');
  let batchStatus: string | null = $state(null);
  let batchError: string | null = $state(null);
  let batchBusy = $state(false);
  let batchProgress: { total: number; submitted: number; failed: number } | null = $state(null);

  async function submitBatch(): Promise<void> {
    const urls = batchText
      .split('\n')
      .map((s) => s.trim())
      .filter((s) => s && !s.startsWith('#'));
    if (urls.length === 0) {
      batchError = 'No URLs found. Paste one URL per line.';
      return;
    }
    batchBusy = true;
    batchError = null;
    batchStatus = `Submitting ${urls.length} URLs…`;
    batchProgress = { total: urls.length, submitted: 0, failed: 0 };
    for (const url of urls) {
      try {
        await api.post<JobAck>('/run', {
          op: 'acquire.url',
          inputs: [],
          params: { url, quality: batchQuality },
        });
        batchProgress = { ...batchProgress, submitted: batchProgress.submitted + 1 };
      } catch {
        batchProgress = { ...batchProgress, failed: batchProgress.failed + 1 };
      }
    }
    batchStatus = `Done. ${batchProgress.submitted} submitted, ${batchProgress.failed} failed.`;
    batchBusy = false;
  }
</script>

<svelte:head>
  <title>media_engine · Ingest</title>
</svelte:head>

<header class="mb-6">
  <h1 class="text-2xl font-semibold mb-1" style="color: var(--text-primary);">Ingest</h1>
  <p class="text-sm" style="color: var(--text-secondary);">
    Pull bytes into the cache. Each tab maps to one <code class="font-mono text-xs">acquire.*</code> op.
  </p>
</header>

<div class="flex gap-1 mb-5 text-sm" role="tablist">
  {#each [
    { id: 'upload', label: 'Upload' },
    { id: 'url', label: 'URL' },
    { id: 'livestream', label: 'Livestream' },
    { id: 'batch', label: 'Batch URLs' },
  ] as tab (tab.id)}
    <button
      type="button"
      role="tab"
      aria-selected={activeTab === tab.id}
      onclick={() => (activeTab = tab.id as Tab)}
      class="px-3 py-1.5 rounded font-medium"
      style={activeTab === tab.id
        ? 'background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-light);'
        : 'color: var(--text-secondary); border: 1px solid transparent;'}
    >
      {tab.label}
    </button>
  {/each}
</div>

<section
  class="p-5 rounded"
  style="background: var(--bg-card); border: 1px solid var(--border-soft);"
>
  {#if activeTab === 'upload'}
    <h2 class="text-base font-semibold mb-3" style="color: var(--text-primary);">Upload a local file</h2>

    <button
      type="button"
      class="block w-full text-center py-8 rounded mb-3 cursor-pointer"
      style="background: var(--bg-alt); border: 2px dashed var(--border-warm); color: var(--text-secondary);"
      ondragover={(e) => e.preventDefault()}
      ondrop={handleDrop}
      onclick={() => document.getElementById('upload-input')?.click()}
    >
      {#if uploadFile}
        <span class="font-mono text-sm" style="color: var(--text-primary);">
          {uploadFile.name}
        </span>
        <span class="block text-xs mt-1">{(uploadFile.size / (1024 * 1024)).toFixed(1)} MB</span>
      {:else}
        <span>Drop a video, audio, or image file here, or click to choose.</span>
      {/if}
    </button>
    <input
      id="upload-input"
      type="file"
      class="hidden"
      onchange={pickFile}
      accept="video/*,audio/*,image/*"
    />

    <div class="flex gap-2">
      <button
        type="button"
        disabled={!uploadFile || uploadBusy}
        onclick={previewUpload}
        class="px-4 py-2 rounded text-sm font-semibold disabled:opacity-50"
        style="background: var(--bg-deep); color: var(--text-primary); border: 1px solid var(--border-light);"
      >
        Probe
      </button>
      <button
        type="button"
        disabled={!uploadFile || uploadBusy}
        onclick={commitUpload}
        class="px-4 py-2 rounded text-sm font-semibold disabled:opacity-50"
        style="background: var(--accent-green); color: var(--text-inverse);"
      >
        Commit
      </button>
    </div>

    {#if uploadStatus}
      <p class="mt-3 text-xs" style="color: var(--text-muted);">{uploadStatus}</p>
    {/if}
    {#if uploadError}
      <p
        class="mt-3 text-xs p-2 rounded"
        style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
      >{uploadError}</p>
    {/if}
    {#if uploadPreview}
      <dl class="mt-4 text-xs grid grid-cols-2 gap-x-4 gap-y-2 font-mono">
        <dt style="color: var(--text-muted);">kind</dt><dd>{uploadPreview.kind}</dd>
        {#if uploadPreview.duration_s !== null}
          <dt style="color: var(--text-muted);">duration</dt><dd>{uploadPreview.duration_s.toFixed(1)} s</dd>
        {/if}
        {#if uploadPreview.codec}
          <dt style="color: var(--text-muted);">codec</dt><dd>{uploadPreview.codec}</dd>
        {/if}
        {#if uploadPreview.width && uploadPreview.height}
          <dt style="color: var(--text-muted);">resolution</dt><dd>{uploadPreview.width}×{uploadPreview.height}</dd>
        {/if}
        <dt style="color: var(--text-muted);">size</dt><dd>{(uploadPreview.size_bytes / (1024 * 1024)).toFixed(2)} MB</dd>
        <dt style="color: var(--text-muted);">sha256</dt><dd>{uploadPreview.sha256_prefix}…</dd>
      </dl>
    {/if}

  {:else if activeTab === 'url'}
    <h2 class="text-base font-semibold mb-3" style="color: var(--text-primary);">Fetch a remote URL</h2>
    <label class="block mb-1 text-xs font-semibold" for="url-input" style="color: var(--text-secondary);">
      URL (anything yt-dlp can resolve)
    </label>
    <input
      id="url-input"
      type="url"
      bind:value={urlInput}
      class="w-full px-3 py-2 rounded font-mono text-sm mb-3"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      placeholder="https://…"
    />
    <label class="block mb-1 text-xs font-semibold" for="url-quality" style="color: var(--text-secondary);">
      Quality
    </label>
    <select
      id="url-quality"
      bind:value={urlQuality}
      class="px-3 py-2 rounded text-sm mb-3"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
    >
      <option value="best">best</option>
      <option value="1080p">1080p</option>
      <option value="720p">720p</option>
      <option value="480p">480p</option>
      <option value="audio">audio only</option>
    </select>

    <div class="flex gap-2">
      <button
        type="button"
        disabled={!urlInput || urlBusy}
        onclick={probeUrl}
        class="px-4 py-2 rounded text-sm font-semibold disabled:opacity-50"
        style="background: var(--bg-deep); color: var(--text-primary); border: 1px solid var(--border-light);"
      >
        Probe
      </button>
      <button
        type="button"
        disabled={!urlInput || urlBusy || !urlProbe?.resolvable}
        onclick={commitUrl}
        class="px-4 py-2 rounded text-sm font-semibold disabled:opacity-50"
        style="background: var(--accent-green); color: var(--text-inverse);"
      >
        Fetch
      </button>
    </div>

    {#if urlError}
      <p
        class="mt-3 text-xs p-2 rounded"
        style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
      >{urlError}</p>
    {/if}
    {#if urlProbe?.resolvable}
      <dl class="mt-4 text-xs grid grid-cols-2 gap-x-4 gap-y-2">
        {#if urlProbe.title}
          <dt style="color: var(--text-muted);">title</dt>
          <dd class="font-medium">{urlProbe.title}</dd>
        {/if}
        {#if urlProbe.uploader}
          <dt style="color: var(--text-muted);">uploader</dt>
          <dd>{urlProbe.uploader}</dd>
        {/if}
        {#if urlProbe.duration_s !== null}
          <dt style="color: var(--text-muted);">duration</dt>
          <dd>{Math.round(urlProbe.duration_s)} s</dd>
        {/if}
        <dt style="color: var(--text-muted);">formats</dt>
        <dd>{urlProbe.formats_available}</dd>
      </dl>
    {/if}

  {:else if activeTab === 'livestream'}
    <h2 class="text-base font-semibold mb-3" style="color: var(--text-primary);">Record an HLS livestream</h2>
    <label class="block mb-1 text-xs font-semibold" for="live-url" style="color: var(--text-secondary);">
      HLS URL (.m3u8 or page URL playwright-hls can sniff)
    </label>
    <input
      id="live-url"
      type="url"
      bind:value={liveUrl}
      class="w-full px-3 py-2 rounded font-mono text-sm mb-3"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
    />

    <div class="grid grid-cols-2 gap-3 mb-3">
      <div>
        <label class="block mb-1 text-xs font-semibold" for="live-max" style="color: var(--text-secondary);">
          Max duration (s)
        </label>
        <input
          id="live-max"
          type="number"
          min="60"
          bind:value={liveMaxDuration}
          class="w-full px-3 py-2 rounded font-mono text-sm"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
        />
      </div>
      <div>
        <label class="block mb-1 text-xs font-semibold" for="live-seg" style="color: var(--text-secondary);">
          Segment seconds (optional)
        </label>
        <input
          id="live-seg"
          type="number"
          min="60"
          bind:value={liveSegmentSeconds}
          class="w-full px-3 py-2 rounded font-mono text-sm"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
          placeholder="(no split)"
        />
      </div>
    </div>

    <button
      type="button"
      disabled={!liveUrl || liveBusy}
      onclick={commitLive}
      class="px-4 py-2 rounded text-sm font-semibold disabled:opacity-50"
      style="background: var(--accent-green); color: var(--text-inverse);"
    >
      Start recording
    </button>
    {#if liveError}
      <p
        class="mt-3 text-xs p-2 rounded"
        style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
      >{liveError}</p>
    {/if}

  {:else if activeTab === 'batch'}
    <h2 class="text-base font-semibold mb-3" style="color: var(--text-primary);">Batch URLs</h2>
    <p class="text-xs mb-3" style="color: var(--text-secondary);">
      Paste one URL per line. The DAG executor fans each out via <code class="font-mono text-xs">acquire.url</code>.
      Lines starting with <code class="font-mono text-xs">#</code> are comments.
    </p>
    <textarea
      bind:value={batchText}
      rows="8"
      class="w-full px-3 py-2 rounded font-mono text-xs mb-3"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
      placeholder="https://example.com/one&#10;https://example.com/two&#10;# a comment&#10;https://example.com/three"
    ></textarea>
    <label class="block mb-1 text-xs font-semibold" for="batch-quality" style="color: var(--text-secondary);">
      Quality (applied to every URL)
    </label>
    <select
      id="batch-quality"
      bind:value={batchQuality}
      class="px-3 py-2 rounded text-sm mb-3"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
    >
      <option value="best">best</option>
      <option value="1080p">1080p</option>
      <option value="720p">720p</option>
      <option value="480p">480p</option>
    </select>
    <button
      type="button"
      disabled={!batchText.trim() || batchBusy}
      onclick={submitBatch}
      class="px-4 py-2 rounded text-sm font-semibold disabled:opacity-50"
      style="background: var(--accent-green); color: var(--text-inverse);"
    >
      Submit
    </button>
    {#if batchStatus}
      <p class="mt-3 text-xs" style="color: var(--text-muted);">{batchStatus}</p>
    {/if}
    {#if batchError}
      <p
        class="mt-3 text-xs p-2 rounded"
        style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
      >{batchError}</p>
    {/if}
    {#if batchProgress}
      <p class="mt-2 text-xs font-mono" style="color: var(--text-secondary);">
        {batchProgress.submitted} ✔ · {batchProgress.failed} ✗ · {batchProgress.total} total
      </p>
    {/if}
  {/if}
</section>
