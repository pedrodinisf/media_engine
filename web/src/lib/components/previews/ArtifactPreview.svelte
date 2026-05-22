<script lang="ts">
  /**
   * Per-kind preview dispatcher.
   *
   * Phase 6 commit 44 ships a native renderer per kind — every preview
   * uses built-in `<video>`/`<audio>`/`<img>` elements and inline JSON
   * for structured kinds. Plan §10 explicitly defers the heavier
   * renderers (wavesurfer waveform, pdf.js, t-SNE embedding projection,
   * OCR bounding boxes) to v1.x; this commit focuses on the contract
   * + reach (every kind has *some* preview).
   */
  import { artifactFileUrl, type Artifact } from '$lib/api/artifacts';

  type Props = { artifact: Artifact };
  let { artifact }: Props = $props();

  function asString(value: unknown): string | null {
    return typeof value === 'string' ? value : null;
  }
</script>

<div class="rounded p-3" style="background: var(--bg-deep); border: 1px solid var(--border-soft);">
  {#if artifact.kind === 'video'}
    <video
      controls
      preload="metadata"
      src={artifactFileUrl(artifact.id)}
      class="w-full max-h-[60vh] rounded"
      style="background: black;"
    >
      <track kind="captions" />
    </video>

  {:else if artifact.kind === 'audio'}
    <audio controls preload="metadata" src={artifactFileUrl(artifact.id)} class="w-full"></audio>

  {:else if artifact.kind === 'image'}
    <img
      src={artifactFileUrl(artifact.id)}
      alt="artifact {artifact.id}"
      class="max-w-full max-h-[60vh] rounded mx-auto"
      style="display: block;"
    />

  {:else if artifact.kind === 'frameset'}
    {@const frames = (artifact.metadata['frames'] ?? []) as Array<{ path?: string; t?: number }>}
    {#if Array.isArray(frames) && frames.length > 0}
      <div class="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-2">
        {#each frames as frame, i (i)}
          <div class="text-xs">
            <div
              class="aspect-video flex items-center justify-center font-mono"
              style="background: var(--bg-alt); color: var(--text-muted); border: 1px solid var(--border-soft); border-radius: 3px;"
              title={frame.path ?? ''}
            >
              {frame.t !== undefined ? `${frame.t.toFixed(2)}s` : `#${i}`}
            </div>
          </div>
        {/each}
      </div>
    {:else}
      <p class="text-xs italic" style="color: var(--text-muted);">FrameSet has no frames metadata.</p>
    {/if}

  {:else if artifact.kind === 'transcript'}
    {@const segments = (artifact.metadata['segments'] ?? []) as Array<{
      start?: number;
      end?: number;
      text?: string;
      speaker_id?: string;
      speaker_name?: string;
    }>}
    {#if Array.isArray(segments) && segments.length > 0}
      <div class="font-mono text-xs space-y-1 max-h-[60vh] overflow-y-auto">
        {#each segments as seg, i (i)}
          <div class="flex gap-3">
            <span class="opacity-60 whitespace-nowrap" style="color: var(--text-muted); min-width: 4rem;">
              {seg.start !== undefined ? seg.start.toFixed(1) : '?'}s
            </span>
            {#if seg.speaker_name || seg.speaker_id}
              <span class="whitespace-nowrap" style="color: var(--accent-green); min-width: 6rem;">
                {seg.speaker_name ?? seg.speaker_id}
              </span>
            {/if}
            <span style="color: var(--text-primary);">{seg.text ?? ''}</span>
          </div>
        {/each}
      </div>
    {:else if asString(artifact.metadata['text'])}
      <pre class="font-mono text-xs whitespace-pre-wrap" style="color: var(--text-primary);">{asString(artifact.metadata['text'])}</pre>
    {:else}
      <p class="text-xs italic" style="color: var(--text-muted);">Transcript has no segments.</p>
    {/if}

  {:else if artifact.kind === 'diarization'}
    {@const turns = (artifact.metadata['segments'] ?? artifact.metadata['turns'] ?? []) as Array<{
      start?: number;
      end?: number;
      speaker_id?: string;
    }>}
    {#if Array.isArray(turns) && turns.length > 0}
      <ol class="font-mono text-xs space-y-0.5 max-h-[40vh] overflow-y-auto">
        {#each turns as turn, i (i)}
          <li>
            <span style="color: var(--text-muted);">
              [{turn.start?.toFixed(1) ?? '?'} – {turn.end?.toFixed(1) ?? '?'}]
            </span>
            <span style="color: var(--accent-green);">{turn.speaker_id ?? ''}</span>
          </li>
        {/each}
      </ol>
    {:else}
      <p class="text-xs italic" style="color: var(--text-muted);">No diarization turns.</p>
    {/if}

  {:else if artifact.kind === 'ocrtext'}
    <pre
      class="font-mono text-xs whitespace-pre-wrap max-h-[60vh] overflow-y-auto"
      style="color: var(--text-primary);"
    >{asString(artifact.metadata['text']) ?? ''}</pre>

  {:else if artifact.kind === 'chunks'}
    {@const chunks = (artifact.metadata['chunks'] ?? []) as Array<{ text?: string; index?: number }>}
    {#if Array.isArray(chunks) && chunks.length > 0}
      <ol class="space-y-2 text-xs max-h-[60vh] overflow-y-auto">
        {#each chunks as chunk, i (i)}
          <li class="p-2 rounded font-mono" style="background: var(--bg-alt); border: 1px solid var(--border-soft);">
            <span style="color: var(--text-muted);">#{chunk.index ?? i}</span>
            <div style="color: var(--text-primary); margin-top: 0.25rem;">{chunk.text ?? ''}</div>
          </li>
        {/each}
      </ol>
    {:else}
      <p class="text-xs italic" style="color: var(--text-muted);">No chunks.</p>
    {/if}

  {:else if artifact.kind === 'embedding'}
    <dl class="text-xs font-mono grid grid-cols-2 gap-x-4 gap-y-1">
      {#each Object.entries(artifact.metadata) as [k, v] (k)}
        {#if typeof v !== 'object' || v === null}
          <dt style="color: var(--text-muted);">{k}</dt>
          <dd>{String(v)}</dd>
        {/if}
      {/each}
    </dl>
    <p class="mt-3 text-xs italic" style="color: var(--text-muted);">
      Vector projection (t-SNE / UMAP) deferred to v1.x — see plan §10.
    </p>

  {:else if artifact.kind === 'markdown'}
    <pre
      class="font-mono text-xs whitespace-pre-wrap max-h-[60vh] overflow-y-auto"
      style="color: var(--text-primary);"
    >{asString(artifact.metadata['text']) ?? ''}</pre>

  {:else if artifact.kind === 'document'}
    {#if asString(artifact.metadata['text'])}
      <pre
        class="font-mono text-xs whitespace-pre-wrap max-h-[60vh] overflow-y-auto"
        style="color: var(--text-primary);"
      >{asString(artifact.metadata['text'])}</pre>
    {:else}
      <p class="text-xs italic" style="color: var(--text-muted);">
        Document has no extracted text. <a href={artifactFileUrl(artifact.id)} download>Download</a>.
      </p>
    {/if}

  {:else if artifact.kind === 'webpage'}
    <dl class="text-xs font-mono grid grid-cols-2 gap-x-4 gap-y-1">
      {#each Object.entries(artifact.metadata).slice(0, 12) as [k, v] (k)}
        {#if typeof v !== 'object' || v === null}
          <dt style="color: var(--text-muted);">{k}</dt>
          <dd class="break-all">{String(v)}</dd>
        {/if}
      {/each}
    </dl>
    <p class="mt-3 text-xs">
      <a href={artifactFileUrl(artifact.id)} target="_blank" rel="noopener" style="color: var(--accent-green);">
        Open raw page bytes →
      </a>
    </p>

  {:else}
    <!-- analysis + session_analysis fall through here -->
    <pre
      class="font-mono text-xs whitespace-pre-wrap max-h-[60vh] overflow-y-auto"
      style="color: var(--text-primary);"
    >{JSON.stringify(artifact.metadata, null, 2)}</pre>
  {/if}
</div>
