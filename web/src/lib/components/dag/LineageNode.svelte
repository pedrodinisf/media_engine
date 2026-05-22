<script lang="ts">
  /**
   * Custom xyflow node for lineage graphs.
   *
   * Renders one artifact: kind label at the top, op + backend below,
   * truncation badge when the walker stopped at max_depth. Whole node
   * is a link to {base}/catalog/{id} so click-to-drill-in works without
   * extra wiring.
   */
  import { base } from '$app/paths';
  import { Handle, Position, type NodeProps } from '@xyflow/svelte';
  import { kindAccent, type FlowNodeData } from './lineage-layout';

  let { data }: NodeProps & { data: FlowNodeData } = $props();

  const accent = $derived(kindAccent(data.kind));
</script>

<a
  href="{base}/catalog/{data.artifact_id}"
  class="block rounded text-xs no-underline"
  style="
    width: 220px;
    height: 80px;
    padding: 8px 10px;
    background: {accent.bg};
    border: 1px solid {accent.border};
    color: var(--text-primary);
    box-shadow: {data.is_root ? '0 0 0 2px var(--accent-green)' : 'none'};
  "
>
  <Handle type="source" position={Position.Bottom} style="opacity: 0;" />
  <Handle type="target" position={Position.Top} style="opacity: 0;" />

  <div
    class="font-mono uppercase tracking-wider"
    style="color: {accent.label}; font-size: 10px;"
  >
    {data.kind}{data.is_root ? ' · root' : ''}
  </div>
  <div class="font-mono mt-0.5" style="font-size: 11px;">
    {data.artifact_id.slice(0, 12)}…
  </div>
  {#if data.op}
    <div class="font-mono mt-1" style="color: var(--text-muted); font-size: 10px;">
      {data.op}{data.backend ? ` · ${data.backend}` : ''}
    </div>
  {/if}
  {#if data.truncated_reason}
    <div
      class="font-mono mt-1"
      style="color: var(--accent-amber); font-size: 10px;"
    >
      … ({data.truncated_reason})
    </div>
  {/if}
</a>
