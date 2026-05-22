<script lang="ts">
  /**
   * Svelte-Flow lineage viewer.
   *
   * Reads the parent → inputs `LineageNode` tree from
   * `GET /artifacts/{id}/lineage`, flattens it through dagre with a
   * bottom-up rank direction, and renders a pan/zoom canvas. Nodes
   * are clickable links — click to drill into the upstream artifact.
   *
   * The same component composition (xyflow viewport + custom node
   * type + dagre layout) is reused by commit 47's profile composer;
   * only the source data and writability differ.
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

  import type { LineageNode } from '$lib/api/lineage';
  import { layoutLineage } from './lineage-layout';
  import LineageNodeView from './LineageNode.svelte';

  type Props = {
    lineage: LineageNode;
    /** Min height of the viewport.  Lineage trees can be deep — let
     *  the caller size to context. */
    minHeight?: string;
  };
  let { lineage, minHeight = '70vh' }: Props = $props();

  const nodeTypes = { lineage: LineageNodeView };

  // Recompute the laid-out graph when the input tree changes. Svelte
  // Flow accepts plain arrays directly (use bind: on the parent for
  // mutability — we're read-only here, so a $derived value is enough).
  const laid = $derived(layoutLineage(lineage));
  let nodes: Node[] = $derived(laid.nodes as Node[]);
  let edges: Edge[] = $derived(
    laid.edges.map((e) => ({
      ...e,
      animated: false,
      style: 'stroke: var(--border-warm);',
    })) as Edge[],
  );
</script>

<div
  class="rounded"
  style="height: {minHeight}; background: var(--bg-page); border: 1px solid var(--border-soft);"
>
  <SvelteFlow
    {nodes}
    {edges}
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
