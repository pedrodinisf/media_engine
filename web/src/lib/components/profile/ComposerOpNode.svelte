<script lang="ts">
  import { Handle, Position, type NodeProps } from '@xyflow/svelte';
  import type { ComposerFlowNodeData } from './composer-layout';

  type Props = NodeProps & {
    data: Extract<ComposerFlowNodeData, { kind: 'op' }> & {
      onSelect?: (nodeId: string) => void;
      isSelected?: boolean;
    };
  };
  let { data }: Props = $props();

  const provider = $derived(data.provider ?? 'unknown');
  const chip = $derived(
    provider === 'cloud'
      ? { label: '☁ cloud', color: 'var(--accent-amber)' }
      : provider === 'local'
        ? { label: '⌂ local', color: 'var(--accent-green)' }
        : provider === 'composite'
          ? { label: '⚙ mixed', color: 'var(--accent-blue)' }
          : null,
  );
  const backendLabel = $derived(
    data.resolved_backend ?? data.backend ?? (provider === 'composite' ? 'composite' : '—'),
  );
  const modelLabel = $derived(
    (data.models ?? [])
      .map((m) => m.value)
      .filter(Boolean)
      .join(', '),
  );
</script>

<button
  type="button"
  class="block rounded text-xs w-full text-left"
  style="
    width: 240px;
    height: 104px;
    padding: 10px 12px;
    background: {data.is_invalid ? 'var(--accent-red-soft)' : 'var(--bg-card)'};
    border: 1px solid {data.is_invalid
      ? 'rgba(220,38,38,0.45)'
      : data.isSelected
        ? 'var(--accent-green)'
        : 'var(--border-warm)'};
    color: var(--text-primary);
    box-shadow: {data.isSelected ? '0 0 0 2px var(--accent-green-soft)' : 'none'};
    cursor: pointer;
  "
  onclick={() => data.onSelect?.(data.id)}
>
  <Handle type="target" position={Position.Top} style="background: var(--accent-green);" />
  <Handle type="source" position={Position.Bottom} style="background: var(--accent-green);" />

  <div
    class="font-mono uppercase tracking-wider truncate"
    style="color: {data.is_invalid ? 'var(--accent-red)' : 'var(--text-muted)'}; font-size: 10px;"
  >
    {data.is_invalid ? 'INVALID · ' : ''}{data.op}
  </div>
  <div class="font-mono mt-1 truncate" style="font-size: 12px;">{data.id}</div>
  <div class="font-mono mt-1 truncate flex items-center gap-1" style="font-size: 10px;">
    {#if chip}
      <span style="color: {chip.color}; font-weight: 600;">{chip.label}</span>
    {/if}
    <span style="color: var(--text-muted);">
      {backendLabel} · {data.params_count}p
    </span>
  </div>
  {#if data.requirement_hint}
    <div class="truncate" style="color: var(--accent-red); font-size: 9px;">
      {data.requirement_hint}
    </div>
  {:else if modelLabel}
    <div class="font-mono truncate" style="color: var(--text-secondary); font-size: 9px;">
      {modelLabel}
    </div>
  {/if}
</button>
