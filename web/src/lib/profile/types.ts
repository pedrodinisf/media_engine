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

export type ProfileSummary = {
  name: string;
  kind: ProfileKind;
  description: string;
  path: string;
  /** Whether the profile ships with the engine (read-only) or lives
   *  in the user's config dir (editable). Server-supplied — no path
   *  heuristic needed. */
  source: 'bundled' | 'user';
};

/** Response from `POST /profiles/validate`. Always 200 — `ok` carries
 *  the verdict. */
export type CompiledNode = {
  id: string;
  op: string;
  backend: string | null;
  inputs: string[];
};

export type ValidateProfileResponse = {
  ok: boolean;
  compiled_nodes: CompiledNode[];
  error_class: string | null;
  message: string | null;
  line: number | null;
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
