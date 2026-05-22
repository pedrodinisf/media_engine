/**
 * Artifact-specific REST helpers + types mirroring the engine's
 * artifacts/base.py + media.py + text.py + analysis.py.
 */

import { getTokenSync } from '$lib/stores/token';

export type ArtifactKind =
  | 'video'
  | 'audio'
  | 'image'
  | 'frameset'
  | 'transcript'
  | 'diarization'
  | 'ocrtext'
  | 'chunks'
  | 'embedding'
  | 'analysis'
  | 'session_analysis'
  | 'markdown'
  | 'document'
  | 'webpage';

export const ARTIFACT_KINDS: readonly ArtifactKind[] = [
  'video',
  'audio',
  'image',
  'frameset',
  'transcript',
  'diarization',
  'ocrtext',
  'chunks',
  'embedding',
  'analysis',
  'session_analysis',
  'markdown',
  'document',
  'webpage',
];

export type Artifact = {
  id: string;
  kind: ArtifactKind;
  path: string;
  metadata: Record<string, unknown>;
  derived_from: readonly string[];
  produced_by: string | null;
  namespace: string;
  created_at: string;
};

export type ArtifactPage = {
  items: Artifact[];
  limit: number;
  next_offset: number | null;
};

/** Build a /file URL that auth-injects the bearer token via ?token=.
 *  Used directly as a <video>/<audio>/<img> src so the browser can do
 *  Range requests against the same FastAPI route, no JS streaming.
 */
export function artifactFileUrl(id: string): string {
  const token = getTokenSync();
  const base = `/artifacts/${encodeURIComponent(id)}/file`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}
