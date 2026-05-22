/**
 * YAML ↔ typed graph round-trip.
 *
 * The `yaml` JS library's `Document` model lets us:
 *  - parse a hand-written YAML file into an AST that preserves
 *    comments + key order;
 *  - mutate specific subtrees (rename a node, change a param);
 *  - serialize back so the un-touched regions come out byte-identical.
 *
 * The composer (`/ui/profiles/[name]`) renders from the parsed
 * `Document`; edits go through these mutators; serialization produces
 * the canonical YAML the API + cache see.
 *
 * Plain `yaml.parse` returns a JS object — convenient but lossy. We
 * reach for the `Document` for the round-trip path, and fall back to
 * `parse()` for read-only consumers (e.g. the validation panel).
 */

import {
  parse as yamlParse,
  parseDocument,
  stringify as yamlStringify,
  type Document,
} from 'yaml';
import type {
  GraphNodeSpec,
  InputSpec,
  PipelineProfile,
  ProfileKind,
} from './types';

export type ParsedGraph = {
  kind: ProfileKind | 'unknown';
  name: string;
  description: string;
  inputs: InputSpec[];
  nodes: GraphNodeSpec[];
  outputs: string[];
  /** True when the YAML body is recognisable as a `kind: pipeline`. */
  isPipeline: boolean;
};

export const EMPTY_GRAPH: ParsedGraph = {
  kind: 'unknown',
  name: '',
  description: '',
  inputs: [],
  nodes: [],
  outputs: [],
  isPipeline: false,
};

/** Parse YAML text into a typed graph view. Lossy — comments + key
 *  order are NOT preserved. Use {@link parseDocument} from `yaml`
 *  directly when round-trip fidelity matters. */
export function parseProfileText(text: string): ParsedGraph {
  if (!text.trim()) return EMPTY_GRAPH;
  let raw: unknown;
  try {
    raw = yamlParse(text);
  } catch {
    return EMPTY_GRAPH;
  }
  if (!raw || typeof raw !== 'object') return EMPTY_GRAPH;
  const obj = raw as Record<string, unknown>;
  const kindRaw = typeof obj.kind === 'string' ? obj.kind : 'pipeline';
  const kind: ProfileKind | 'unknown' =
    kindRaw === 'pipeline' || kindRaw === 'prompt' ? kindRaw : 'unknown';

  const inputs: InputSpec[] = Array.isArray(obj.inputs)
    ? (obj.inputs as unknown[])
        .filter((i): i is Record<string, unknown> => typeof i === 'object' && i !== null)
        .map((i) => ({
          name: typeof i.name === 'string' ? i.name : '',
          kind: typeof i.kind === 'string' ? i.kind : '',
        }))
    : [];

  const nodes: GraphNodeSpec[] = Array.isArray(obj.graph)
    ? (obj.graph as unknown[])
        .filter((n): n is Record<string, unknown> => typeof n === 'object' && n !== null)
        .map((n) => normaliseNode(n))
    : [];

  const outputs: string[] = Array.isArray(obj.outputs)
    ? (obj.outputs as unknown[]).filter((s): s is string => typeof s === 'string')
    : [];

  return {
    kind,
    name: typeof obj.name === 'string' ? obj.name : '',
    description: typeof obj.description === 'string' ? obj.description : '',
    inputs,
    nodes,
    outputs,
    isPipeline: kind === 'pipeline' && nodes.length > 0,
  };
}

function normaliseNode(raw: Record<string, unknown>): GraphNodeSpec {
  let inputs: Record<string, string> | string[] = {};
  if (Array.isArray(raw.inputs)) {
    inputs = (raw.inputs as unknown[])
      .filter((s): s is string => typeof s === 'string');
  } else if (raw.inputs && typeof raw.inputs === 'object') {
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(raw.inputs)) {
      if (typeof v === 'string') out[k] = v;
    }
    inputs = out;
  }
  const params: Record<string, unknown> =
    raw.params && typeof raw.params === 'object'
      ? { ...(raw.params as Record<string, unknown>) }
      : {};
  return {
    id: typeof raw.id === 'string' ? raw.id : '',
    op: typeof raw.op === 'string' ? raw.op : '',
    inputs,
    params,
    backend: typeof raw.backend === 'string' ? raw.backend : null,
    depends_on: Array.isArray(raw.depends_on)
      ? (raw.depends_on as unknown[]).filter((s): s is string => typeof s === 'string')
      : [],
  };
}

/** Build a `yaml.Document` for round-trip mutation. The composer keeps
 *  this around per workspace session; the editor + canvas both write
 *  back through {@link mutateNode} / {@link addNode} / {@link removeNode}. */
export function loadDocument(text: string): Document {
  return parseDocument(text || '{}');
}

/** Replace one graph node by id, mutating the Document AST in place.
 *  Preserves comments + key order on the rest of the file. Returns
 *  `true` when the node was found + updated. */
export function mutateNode(doc: Document, nodeId: string, patch: Partial<GraphNodeSpec>): boolean {
  const graph = doc.getIn(['graph']);
  // Resolve `graph` via the yaml lib's array-like accessor; if the
  // file isn't a pipeline / has no graph, bail.
  if (!graph || typeof graph !== 'object' || !('items' in graph)) return false;
  // The graph is a yaml.YAMLSeq — its items are yaml.YAMLMap-like.
  const items = (graph as { items: unknown[] }).items;
  for (let i = 0; i < items.length; i++) {
    const idNode = doc.getIn(['graph', i, 'id']);
    if (typeof idNode === 'string' && idNode === nodeId) {
      for (const [k, v] of Object.entries(patch)) {
        if (v === undefined) continue;
        doc.setIn(['graph', i, k], v);
      }
      return true;
    }
  }
  return false;
}

/** Remove a node by id. Returns `true` when found + removed. */
export function removeNode(doc: Document, nodeId: string): boolean {
  const graph = doc.getIn(['graph']);
  if (!graph || typeof graph !== 'object' || !('items' in graph)) return false;
  const items = (graph as { items: unknown[] }).items;
  for (let i = 0; i < items.length; i++) {
    const idNode = doc.getIn(['graph', i, 'id']);
    if (typeof idNode === 'string' && idNode === nodeId) {
      doc.deleteIn(['graph', i]);
      return true;
    }
  }
  return false;
}

/** Append a new node to the graph. */
export function addNode(doc: Document, spec: GraphNodeSpec): void {
  const graph = doc.getIn(['graph']);
  if (!graph || typeof graph !== 'object' || !('items' in graph)) {
    doc.setIn(['graph'], [graphNodeToPlain(spec)]);
    return;
  }
  doc.addIn(['graph'], graphNodeToPlain(spec));
}

function graphNodeToPlain(spec: GraphNodeSpec): Record<string, unknown> {
  const out: Record<string, unknown> = { id: spec.id, op: spec.op };
  if (Array.isArray(spec.inputs) ? spec.inputs.length > 0 : Object.keys(spec.inputs).length > 0) {
    out.inputs = spec.inputs;
  }
  if (Object.keys(spec.params).length > 0) out.params = spec.params;
  if (spec.backend) out.backend = spec.backend;
  if (spec.depends_on.length > 0) out.depends_on = spec.depends_on;
  return out;
}

/** Materialize the Document back to YAML text. */
export function serializeDocument(doc: Document): string {
  return doc.toString();
}

/** Quick-and-dirty pipeline → YAML shortcut (no comment preservation
 *  because we have no source document). Used by the composer when
 *  rendering a profile assembled entirely via drag-and-drop. */
export function pipelineToYaml(profile: Partial<PipelineProfile>): string {
  const cleaned: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(profile)) {
    if (v === undefined || v === null || v === '') continue;
    if (Array.isArray(v) && v.length === 0) continue;
    cleaned[k] = v;
  }
  return yamlStringify(cleaned);
}
