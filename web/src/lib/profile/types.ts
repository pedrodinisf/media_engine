/**
 * Typed shapes mirroring `media_engine/profiles/schema.py` and the
 * `POST /profiles/validate` response. The Web UI carries these
 * directly — there's no Python ⇄ JS adapter beyond JSON.
 */

export type ProfileKind = 'pipeline' | 'prompt';

export type InputSpec = {
  name: string;
  kind: string; // Kind enum value (lowercase string)
};

export type GraphNodeSpec = {
  id: string;
  op: string;
  /** Either positional list or named-input dict; the loader normalises
   *  both to a list of source/node refs at compile time. */
  inputs: Record<string, string> | string[];
  params: Record<string, unknown>;
  backend: string | null;
  depends_on: string[];
};

export type PipelineProfile = {
  profile_schema_version: string;
  name: string;
  kind: 'pipeline';
  description: string;
  inputs: InputSpec[];
  graph: GraphNodeSpec[];
  outputs: string[];
};

export type PromptProfile = {
  profile_schema_version: string;
  name: string;
  kind: 'prompt';
  description: string;
  default_op: string;
  default_backend: string | null;
  schema_path: string | null;
  body: string;
};

/** Provider a model / backend routes to. `composite` = an embedded op with
 *  no single backend (its per-model providers carry the detail). */
export type Provider = 'cloud' | 'local' | 'composite' | 'unknown';

/** A model-typed param on a node + which provider it routes to. */
export type ModelFieldRef = {
  name: string;
  value: string | null;
  provider: 'cloud' | 'local' | 'unknown';
};

/** Compact per-profile "what does it use?" summary for the list cards. */
export type ProfileDigest = {
  models: { name: string; provider: 'cloud' | 'local' | 'unknown' }[];
  providers: string[];
  requirement_hints: string[];
};

export type ProfileSummary = {
  name: string;
  kind: ProfileKind;
  description: string;
  path: string;
  /** Whether the profile ships with the engine (read-only) or lives
   *  in the user's config dir (editable). Server-supplied — no path
   *  heuristic needed. */
  source: 'bundled' | 'user';
  /** Phase 8 — models/providers/requirement hints for the card badges. */
  digest?: ProfileDigest | null;
};

/** Response from `POST /profiles/validate`. Always 200 — `ok` carries
 *  the verdict. Nodes carry Phase-8 model/provider enrichment. */
export type CompiledNode = {
  id: string;
  op: string;
  backend: string | null;
  inputs: string[];
  resolved_backend?: string | null;
  provider?: Provider;
  models?: ModelFieldRef[];
  requirement_hint?: string | null;
};

export type ValidateProfileResponse = {
  ok: boolean;
  compiled_nodes: CompiledNode[];
  error_class: string | null;
  message: string | null;
  line: number | null;
};

/** Per-node result of `POST /pipelines/preview` (a pipeline preflight). */
export type NodePreview = {
  id: string;
  op: string;
  backend: string | null;
  embedded: boolean;
  cached: boolean;
  resolvable: boolean;
  models: ModelFieldRef[];
  estimate_seconds_local: number;
  estimate_cost_cents: number;
  estimate_tokens_in: number;
  estimate_tokens_out: number;
  feasibility_error: string | null;
};

export type PipelinePreviewResponse = {
  ok: boolean;
  nodes: NodePreview[];
  total_seconds_local: number;
  total_cost_cents: number;
  total_tokens_in: number;
  total_tokens_out: number;
  error_class: string | null;
  message: string | null;
};

/** Skeleton YAML for the "+ New" button on `/ui/profiles`. */
export const BLANK_PIPELINE_YAML = `name: untitled-pipeline
kind: pipeline
description: ""

inputs:
  - { name: source, kind: video }

graph:
  - id: audio
    op: video.extract_audio
    inputs: { in: source }

outputs:
  - audio
`;
