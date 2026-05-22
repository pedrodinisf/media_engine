/**
 * REST helpers backing the profile workspace.
 *
 * Mirrors `media_engine/api/routes.py:ValidateProfileResponse` +
 * `ProfileSummary` + `POST /profiles` + `DELETE /profiles/{name}`.
 */

import { api } from '$lib/api/client';
import type {
  PipelineProfile,
  ProfileSummary,
  PromptProfile,
  ValidateProfileResponse,
} from './types';

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
