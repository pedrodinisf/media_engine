/**
 * Flatten a `LineageNode` tree into the Node + Edge arrays Svelte Flow
 * expects, then run dagre to assign x/y coordinates.
 *
 * The lineage tree is parent → inputs, i.e. each node's `inputs` are
 * the artifacts it was derived from. Visually we render bottom-to-top:
 * the root (the artifact the user opened) sits at the bottom and its
 * upstream ancestors stack above. Dagre's `BT` rank direction gives us
 * that for free.
 *
 * Each artifact id appears at most once in the graph even if multiple
 * paths in the tree converge on it (content-addressed cache → same id
 * means same artifact). The dedupe keeps the diagram readable when
 * fan-in is heavy.
 */

import dagre from '@dagrejs/dagre';
import type { LineageNode } from '$lib/api/lineage';

export type FlowNodeData = {
  artifact_id: string;
  kind: string;
  op?: string | null;
  backend?: string | null;
  truncated_reason?: 'max_depth' | 'cycle' | null;
  is_root: boolean;
};

export type FlowNode = {
  id: string;
  position: { x: number; y: number };
  data: FlowNodeData;
  /** Plain xyflow node type — we register a custom "lineage" node in the
   *  Svelte component. */
  type: 'lineage';
  width: number;
  height: number;
};

export type FlowEdge = {
  id: string;
  source: string;
  target: string;
};

const NODE_WIDTH = 220;
const NODE_HEIGHT = 80;

/** Walk the tree, collecting (deduped) nodes + edges. */
function flattenTree(
  root: LineageNode,
): { nodes: Map<string, FlowNodeData>; edges: FlowEdge[] } {
  const nodes = new Map<string, FlowNodeData>();
  const edges: FlowEdge[] = [];
  const seenEdges = new Set<string>();

  function visit(node: LineageNode, isRoot: boolean): void {
    const existing = nodes.get(node.artifact_id);
    if (!existing) {
      nodes.set(node.artifact_id, {
        artifact_id: node.artifact_id,
        kind: node.kind,
        op: node.op ?? null,
        backend: node.backend ?? null,
        truncated_reason: node.truncated_reason ?? null,
        is_root: isRoot,
      });
    } else if (isRoot) {
      // The recursive walker may revisit the root via a fan-in path —
      // make sure the is_root flag survives.
      existing.is_root = true;
    }
    for (const input of node.inputs ?? []) {
      const edgeId = `${input.artifact_id}->${node.artifact_id}`;
      if (!seenEdges.has(edgeId)) {
        seenEdges.add(edgeId);
        edges.push({ id: edgeId, source: input.artifact_id, target: node.artifact_id });
      }
      visit(input, false);
    }
  }

  visit(root, true);
  return { nodes, edges };
}

/** Run dagre over the flattened graph, return Svelte Flow nodes + edges. */
export function layoutLineage(
  root: LineageNode,
  { rankdir = 'BT' }: { rankdir?: 'BT' | 'TB' | 'LR' | 'RL' } = {},
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const { nodes: nodeData, edges } = flattenTree(root);

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir, nodesep: 24, ranksep: 40, marginx: 16, marginy: 16 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const [id] of nodeData) {
    g.setNode(id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  const flowNodes: FlowNode[] = [];
  for (const [id, data] of nodeData) {
    const pos = g.node(id);
    flowNodes.push({
      id,
      // Dagre returns center positions; xyflow wants top-left.
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      data,
      type: 'lineage',
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    });
  }

  return { nodes: flowNodes, edges };
}

/** Color hint for a node, keyed by kind family. Returns a CSS color
 *  variable name + accent.  The Svelte component reads these into
 *  inline styles. */
export function kindAccent(kind: string): { bg: string; border: string; label: string } {
  if (['video', 'audio', 'image', 'frameset'].includes(kind)) {
    return {
      bg: 'var(--bg-card)',
      border: 'var(--accent-green-line)',
      label: 'var(--accent-green)',
    };
  }
  if (['analysis', 'session_analysis', 'embedding'].includes(kind)) {
    return {
      bg: 'var(--bg-card)',
      border: 'var(--accent-amber-line)',
      label: 'var(--accent-amber)',
    };
  }
  // text / document / chunks / webpage / etc.
  return {
    bg: 'var(--bg-alt)',
    border: 'var(--border-warm)',
    label: 'var(--text-secondary)',
  };
}
