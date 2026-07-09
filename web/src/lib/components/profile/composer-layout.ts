/**
 * Lay out a parsed profile graph for Svelte Flow's composer canvas.
 *
 * The profile DAG has two node families:
 *  - **inputs** (declared `inputs:` in the profile YAML — source
 *    artifacts the user picks at run time);
 *  - **ops** (the `graph:` list — `op.<verb>` invocations).
 *
 * Edges flow inputs → first wave → … → outputs. Dagre's `TB` rank
 * direction renders the source row at the top and outputs at the
 * bottom, matching how pipelines read in YAML.
 */

import dagre from '@dagrejs/dagre';
import type {
  CompiledNode,
  GraphNodeSpec,
  InputSpec,
  ModelFieldRef,
  Provider,
} from '$lib/profile/types';

export type ComposerFlowNodeData =
  | {
      kind: 'input';
      name: string;
      artifact_kind: string;
    }
  | {
      kind: 'op';
      id: string;
      op: string;
      backend: string | null;
      inputs: readonly string[];
      params_count: number;
      is_invalid?: boolean;
      // Phase 8 — model/provider enrichment (from /profiles/validate).
      resolved_backend?: string | null;
      provider?: Provider;
      models?: ModelFieldRef[];
      requirement_hint?: string | null;
    };

export type ComposerFlowNode = {
  id: string;
  position: { x: number; y: number };
  data: ComposerFlowNodeData;
  type: 'composer-op' | 'composer-input';
  width: number;
  height: number;
};

export type ComposerFlowEdge = {
  id: string;
  source: string;
  target: string;
};

const NODE_WIDTH = 240;
const NODE_HEIGHT = 104;

function inputRefs(node: GraphNodeSpec): string[] {
  if (Array.isArray(node.inputs)) return node.inputs;
  return Object.values(node.inputs);
}

export function layoutComposer(
  inputs: InputSpec[],
  nodes: GraphNodeSpec[],
  invalidNodeIds: ReadonlySet<string> = new Set(),
  enrichment: ReadonlyMap<string, CompiledNode> = new Map(),
): { nodes: ComposerFlowNode[]; edges: ComposerFlowEdge[] } {
  const flowNodes: Map<string, { type: 'composer-op' | 'composer-input'; data: ComposerFlowNodeData }> = new Map();
  const edges: ComposerFlowEdge[] = [];
  const seenEdge = new Set<string>();

  for (const inp of inputs) {
    const key = `src:${inp.name}`;
    flowNodes.set(key, {
      type: 'composer-input',
      data: { kind: 'input', name: inp.name, artifact_kind: inp.kind },
    });
  }

  for (const node of nodes) {
    const key = `op:${node.id}`;
    const enriched = enrichment.get(node.id);
    flowNodes.set(key, {
      type: 'composer-op',
      data: {
        kind: 'op',
        id: node.id,
        op: node.op,
        backend: node.backend,
        inputs: inputRefs(node),
        params_count: Object.keys(node.params).length,
        is_invalid: invalidNodeIds.has(node.id),
        resolved_backend: enriched?.resolved_backend ?? null,
        provider: enriched?.provider ?? 'unknown',
        models: enriched?.models ?? [],
        requirement_hint: enriched?.requirement_hint ?? null,
      },
    });
    for (const ref of inputRefs(node)) {
      const sourceKey = inputs.some((i) => i.name === ref) ? `src:${ref}` : `op:${ref}`;
      const edgeId = `${sourceKey}->${key}`;
      if (!seenEdge.has(edgeId)) {
        seenEdge.add(edgeId);
        edges.push({ id: edgeId, source: sourceKey, target: key });
      }
    }
    for (const dep of node.depends_on) {
      const sourceKey = `op:${dep}`;
      const edgeId = `${sourceKey}->${key}`;
      if (!seenEdge.has(edgeId)) {
        seenEdge.add(edgeId);
        edges.push({ id: edgeId, source: sourceKey, target: key });
      }
    }
  }

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'TB', nodesep: 32, ranksep: 56, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const id of flowNodes.keys()) {
    g.setNode(id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }
  dagre.layout(g);

  const out: ComposerFlowNode[] = [];
  for (const [id, meta] of flowNodes) {
    const pos = g.node(id);
    out.push({
      id,
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      data: meta.data,
      type: meta.type,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    });
  }
  return { nodes: out, edges };
}
