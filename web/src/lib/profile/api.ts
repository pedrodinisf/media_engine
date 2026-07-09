/**
 * REST helpers backing the profile workspace.
 *
 * Mirrors `media_engine/api/routes.py:ValidateProfileResponse` +
 * `ProfileSummary` + `POST /profiles` + `DELETE /profiles/{name}`.
 */

import { api } from '$lib/api/client';
import type { ParamsSchema } from '$lib/components/forms/schema';
import type {
  PipelineProfile,
  PipelinePreviewResponse,
  ProfileSummary,
  PromptProfile,
  ValidateProfileResponse,
} from './types';

/** Source spec for a pipeline run / preview: declared input name → artifact id. */
export type PipelineSource = { name: string; artifact_id: string };

/** Subset of `GET /operations/{name}` the workspace consumes: the JSON Schema
 *  that drives the per-node param form. */
export type OperationDetail = {
  name: string;
  params_schema: ParamsSchema;
  backends: string[];
  default_backend: string | null;
};

export function getOperationDetail(op: string): Promise<OperationDetail> {
  return api.get<OperationDetail>(`/operations/${encodeURIComponent(op)}`);
}

/**
 * Preflight a pipeline (inline YAML + picked sources) without running it —
 * per-node backend/models/cost + feasibility. The workspace Run button calls
 * this first and blocks on any `feasibility_error`.
 */
export function previewPipeline(
  pipeline_yaml: string,
  sources: PipelineSource[],
): Promise<PipelinePreviewResponse> {
  return api.post<PipelinePreviewResponse>('/pipelines/preview', {
    pipeline_yaml,
    sources,
  });
}

export function listProfiles(): Promise<ProfileSummary[]> {
  return api.get<ProfileSummary[]>('/profiles');
}

export function getProfile(
  name: string,
): Promise<(PipelineProfile | PromptProfile) & { _source_path: string }> {
  return api.get(`/profiles/${encodeURIComponent(name)}`);
}

export function saveProfile(
  body: PipelineProfile | PromptProfile,
): Promise<ProfileSummary> {
  return api.post<ProfileSummary>('/profiles', body);
}

export function deleteProfile(name: string): Promise<void> {
  return api.delete<void>(`/profiles/${encodeURIComponent(name)}`);
}

export function validateProfile(
  pipeline_yaml: string,
): Promise<ValidateProfileResponse> {
  return api.post<ValidateProfileResponse>('/profiles/validate', { pipeline_yaml });
}

/**
 * Mirror of the server's `_PROFILE_NAME_RE` (lowercase + digits +
 * `-` / `_`; 1–64 chars; must start with a letter or digit). Used by
 * the fork modal to validate input before the round-trip — gives the
 * user instant feedback rather than waiting for a 400.
 */
export const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export function isValidProfileName(name: string): boolean {
  return PROFILE_NAME_RE.test(name);
}

/**
 * Build a fork payload: take a bundled profile body and re-target it
 * under a new kebab-case name. The server's POST /profiles persists
 * the result to `{config_dir}/profiles/`, where it shadows the
 * bundled original at discovery time.
 */
export function forkPayload(
  original: PipelineProfile | PromptProfile,
  newName: string,
): PipelineProfile | PromptProfile {
  if (!isValidProfileName(newName)) {
    throw new Error(
      `invalid profile name ${JSON.stringify(newName)}: must match ${PROFILE_NAME_RE.source}`,
    );
  }
  if (original.kind === 'pipeline') {
    return { ...original, name: newName };
  }
  return { ...original, name: newName };
}
