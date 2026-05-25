<script lang="ts">
  /**
   * media_engine brand mark — "replicated spark".
   *
   * Three left-facing chevron pulses tracing a horizontal axis with
   * cascading opacity (1.00 → 0.55 → 0.25). Reads as "one signal
   * becoming many" — the engine's DAG-of-ops semantic.
   *
   * Uses ``currentColor`` so the mark inherits whatever ``color`` the
   * containing element sets. On the dark header it renders off-white
   * via ``text-text-inverse``; on a light surface it would pick up
   * ``--accent-green``. Single-color by design so the silhouette
   * stays crisp at favicon sizes.
   */
  type Props = { size?: number; class?: string };
  let { size = 20, class: cls = '' }: Props = $props();
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="currentColor"
  stroke="none"
  class={cls}
  aria-hidden="true"
  data-testid="brand-logo"
>
  <path d="M3 12 L8 6 L8 18 Z" opacity="1.00" />
  <path d="M11 12 L16 6 L16 18 Z" opacity="0.55" />
  <path d="M19 12 L22 9 L22 15 Z" opacity="0.25" />
</svg>
