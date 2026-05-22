<script lang="ts">
  /**
   * Visual DAG composer — Svelte Flow canvas + op palette.
   *
   * Reads a parsed profile (inputs + nodes) and lays it out via dagre.
   * Nodes are clickable to surface a per-node editor on the right; the
   * op palette down the left lets the user add a new op-node by
   * clicking. Validation feedback (red border) flows through the
   * `invalidNodeIds` prop so the workspace can drive it from
   * `POST /profiles/validate`.
   *
   * Drag-and-drop wiring + free-position layout are deferred — for v1
   * we re-run dagre on every edit so the layout stays readable.
   */
  import {
    SvelteFlow,
    Background,
    Controls,
    MiniMap,
    type Node,
    type Edge,
  } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';

  import type { GraphNodeSpec, InputSpec } from '$lib/profile/types';
  import { layoutComposer } from './composer-layout';
  import ComposerOpNode from './ComposerOpNode.svelte';
  import ComposerInputNode from './ComposerInputNode.svelte';

  type Props = {
    inputs: readonly InputSpec[];
    nodes: readonly GraphNodeSpec[];
    selectedNodeId?: string | null;
    invalidNodeIds?: ReadonlySet<string>;
    /** Op-name palette — typically every entry of `GET /operations`
     *  whose `input_kinds` is empty or matches the available input
     *  kinds. */
    opPalette: readonly string[];
    onSelectNode: (id: string) => void;
    onAddOp: (op: string) => void;
    minHeight?: string;
  };

  let {
    inputs,
    nodes,
    selectedNodeId = null,
    invalidNodeIds = new Set<string>(),
    opPalette,
    onSelectNode,
    onAddOp,
    minHeight = '60vh',
  }: Props = $props();

  const nodeTypes = {
    'composer-op': ComposerOpNode,
    'composer-input': ComposerInputNode,
  };

  const laid = $derived(layoutComposer([...inputs], [...nodes], invalidNodeIds));
  const flowNodes = $derived<Node[]>(
    laid.nodes.map((n) => ({
      ...n,
      data:
        n.data.kind === 'op'
          ? {
              ...n.data,
              onSelect: onSelectNode,
              isSelected: n.data.id === selectedNodeId,
            }
          : n.data,
    })) as Node[],
  );
  const flowEdges = $derived<Edge[]>(
    laid.edges.map((e) => ({
      ...e,
      animated: false,
      style: 'stroke: var(--border-warm);',
    })) as Edge[],
  );

  let paletteFilter = $state('');
  const visiblePalette = $derived(
    paletteFilter
      ? opPalette.filter((op) => op.toLowerCase().includes(paletteFilter.toLowerCase()))
      : opPalette,
  );
</script>

<div class="flex gap-3" style="min-height: {minHeight};">
  <aside
    class="w-56 shrink-0 rounded p-3 overflow-y-auto"
    style="background: var(--bg-card); border: 1px solid var(--border-soft); max-height: {minHeight};"
  >
    <div class="text-xs font-semibold uppercase mb-2" style="color: var(--text-muted);">
      Op palette
    </div>
    <input
      type="search"
      bind:value={paletteFilter}
      placeholder="filter…"
      class="w-full px-2 py-1 mb-2 rounded text-xs font-mono"
      style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
    />
    <ul class="text-xs">
      {#each visiblePalette as op (op)}
        <li>
          <button
            type="button"
            onclick={() => onAddOp(op)}
            class="block w-full text-left px-2 py-1 rounded font-mono"
            style="color: var(--text-secondary);"
            title="Append a new node calling {op}"
          >
            + {op}
          </button>
        </li>
      {/each}
      {#if visiblePalette.length === 0}
        <li class="text-xs italic" style="color: var(--text-muted);">no matches</li>
      {/if}
    </ul>
  </aside>

  <div
    class="flex-1 rounded overflow-hidden"
    style="background: var(--bg-page); border: 1px solid var(--border-soft);"
  >
    <SvelteFlow
      nodes={flowNodes}
      edges={flowEdges}
      {nodeTypes}
      fitView
      proOptions={{ hideAttribution: true }}
      minZoom={0.2}
      maxZoom={2}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={true}
    >
      <Background />
      <Controls showLock={false} />
      <MiniMap pannable zoomable />
    </SvelteFlow>
  </div>
</div>
