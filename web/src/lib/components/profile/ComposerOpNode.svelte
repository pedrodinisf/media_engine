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
</script>

<button
  type="button"
  class="block rounded text-xs w-full text-left"
  style="
    width: 240px;
    height: 88px;
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
  <div class="font-mono mt-1 truncate" style="color: var(--text-muted); font-size: 10px;">
    {data.backend ?? '—'} · {data.params_count} param{data.params_count === 1 ? '' : 's'}
  </div>
</button>
