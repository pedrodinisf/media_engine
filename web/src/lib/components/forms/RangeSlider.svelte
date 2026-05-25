<script lang="ts">
  /**
   * Dual-handle range slider — pick a [start, end) window from
   * 0 → ``duration``. Used by the Run panel for audio ops to bound
   * a transcribe / diarize to a sub-range without first creating a
   * trimmed artifact.
   *
   * Two stacked native ``<input type=range>`` controls share min/max
   * and synchronize state via the ``onChange`` prop. ``startValue ===
   * null`` is interpreted as "from beginning" (0); ``endValue === null``
   * as "to end" (= ``duration``). The component renders a labelled
   * track and a "Use full range" reset button.
   *
   * Why two range inputs rather than a custom slider widget: zero
   * dependencies, native keyboard a11y (←/→ for fine-tune, page-up/
   * down for jumps), works under prefers-reduced-motion, ships in
   * every browser. The visual overlap (start slider stacked above
   * end slider) is fine for a v1 — operator can always edit the
   * exact numeric value in the SchemaForm fields below.
   */
  import { formatDuration } from '$lib/format/duration';

  type Props = {
    /** Total audio length in seconds. Must be > 0. */
    duration: number;
    /** Current start of the selected window, or null for "from 0". */
    startValue: number | null;
    /** Current end of the selected window, or null for "to duration". */
    endValue: number | null;
    /** Fires on every drag; both args may be null when at extremes. */
    onChange: (start: number | null, end: number | null) => void;
  };

  let { duration, startValue, endValue, onChange }: Props = $props();

  // Effective values for the slider widgets — null collapses to the
  // axis extremes so the handles always have somewhere to live.
  const effStart = $derived(startValue ?? 0);
  const effEnd = $derived(endValue ?? duration);
  const windowLen = $derived(Math.max(0, effEnd - effStart));

  function onStartInput(e: Event): void {
    const next = Number((e.target as HTMLInputElement).value);
    // Don't let start cross end; force at least 1s of window.
    const clamped = Math.min(next, effEnd - 1);
    onChange(
      clamped <= 0 ? null : clamped,
      endValue,
    );
  }

  function onEndInput(e: Event): void {
    const next = Number((e.target as HTMLInputElement).value);
    const clamped = Math.max(next, effStart + 1);
    onChange(
      startValue,
      clamped >= duration ? null : clamped,
    );
  }

  function reset(): void {
    onChange(null, null);
  }

  const isFullRange = $derived(startValue === null && endValue === null);
</script>

<div
  class="rounded p-3 mb-3"
  style="background: var(--bg-page); border: 1px solid var(--border-light);"
>
  <div class="flex items-baseline justify-between mb-2">
    <span class="text-xs font-semibold" style="color: var(--text-secondary);">
      Time range
    </span>
    <span class="text-xs font-mono" style="color: var(--text-muted);">
      {formatDuration(effStart)} → {formatDuration(effEnd)}
      <span style="color: var(--text-secondary);">
        ({formatDuration(windowLen)}
        {isFullRange ? '— full audio' : `of ${formatDuration(duration)}`})
      </span>
    </span>
  </div>

  <label class="block mb-1">
    <span class="sr-only">Start of range</span>
    <input
      type="range"
      min="0"
      max={duration}
      step="1"
      value={effStart}
      oninput={onStartInput}
      class="w-full"
      style="accent-color: var(--accent-green);"
      aria-label="Start time (seconds)"
    />
  </label>

  <label class="block">
    <span class="sr-only">End of range</span>
    <input
      type="range"
      min="0"
      max={duration}
      step="1"
      value={effEnd}
      oninput={onEndInput}
      class="w-full"
      style="accent-color: var(--accent-green);"
      aria-label="End time (seconds)"
    />
  </label>

  <div class="mt-2 flex items-center justify-between text-xs" style="color: var(--text-muted);">
    <span class="font-mono">00:00</span>
    <button
      type="button"
      onclick={reset}
      disabled={isFullRange}
      class="px-2 py-0.5 rounded text-xs font-mono disabled:opacity-50"
      style="background: var(--bg-card); color: var(--text-secondary); border: 1px solid var(--border-light);"
    >
      Use full range
    </button>
    <span class="font-mono">{formatDuration(duration)}</span>
  </div>
</div>
