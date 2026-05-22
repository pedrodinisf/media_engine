<script lang="ts">
  import { Handle, Position, type NodeProps } from '@xyflow/svelte';
  import type { ComposerFlowNodeData } from './composer-layout';

  let { data }: NodeProps & { data: Extract<ComposerFlowNodeData, { kind: 'input' }> } = $props();
</script>

<div
  class="rounded text-xs"
  style="
    width: 240px;
    height: 88px;
    padding: 10px 12px;
    background: var(--bg-card);
    border: 1px dashed var(--accent-green-line);
    color: var(--text-primary);
  "
>
  <Handle type="source" position={Position.Bottom} style="background: var(--accent-green);" />
  <div
    class="font-mono uppercase tracking-wider"
    style="color: var(--accent-green); font-size: 10px;"
  >
    INPUT &middot; {data.artifact_kind}
  </div>
  <div class="font-mono mt-1" style="font-size: 12px;">{data.name}</div>
  <div class="font-mono mt-1" style="color: var(--text-muted); font-size: 10px;">
    bound at run-time
  </div>
</div>
