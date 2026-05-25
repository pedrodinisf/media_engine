<script lang="ts">
  /**
   * media_engine brand mark — "replicated spark".
   *
   * Three left-facing chevrons in green / red / blue (the RGB triad,
   * on-theme for a media-processing engine that splits signals into
   * channels). Each chevron animates a 2.4s `pulse` keyframe with a
   * 0.3s stagger so the colors cascade like a propagating signal —
   * "one signal becoming many," matching the engine's DAG-of-ops
   * semantic.
   *
   * Single-source-of-truth geometry: also used inline in
   * docs/quickstart.html and in web/static/favicon.svg. Keep all
   * three in sync.
   */
  type Props = { size?: number; class?: string };
  let { size = 20, class: cls = '' }: Props = $props();
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  stroke="none"
  class="brand-mark {cls}"
  aria-hidden="true"
  data-testid="brand-logo"
>
  <path d="M3 12 L8 6 L8 18 Z"   fill="var(--accent-green)" />
  <path d="M11 12 L16 6 L16 18 Z" fill="var(--accent-red)" />
  <path d="M19 12 L22 9 L22 15 Z" fill="var(--accent-blue)" />
</svg>

<style>
  /*
   * Path-level pulse. Each chevron fades to ~32% opacity at the
   * trough then back to full saturation, with a 0.3s offset per path
   * so the three pulses chase each other rather than blinking in
   * unison. transform-origin is set so any future scale-pulse
   * variants centre on each chevron's own midpoint.
   */
  .brand-mark path { transform-origin: center; }
  .brand-mark path:nth-child(1) { animation: brand-pulse 2.4s infinite; }
  .brand-mark path:nth-child(2) { animation: brand-pulse 2.4s infinite 0.3s; }
  .brand-mark path:nth-child(3) { animation: brand-pulse 2.4s infinite 0.6s; }

  @keyframes brand-pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.32; }
  }

  /* Respect reduced-motion preferences — disable the pulse entirely
   * but keep the chevrons fully visible. */
  @media (prefers-reduced-motion: reduce) {
    .brand-mark path { animation: none; }
  }
</style>
