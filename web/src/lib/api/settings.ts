/**
 * Typed REST helpers for the Settings panel.
 *
 * Mirrors media_engine/api/plugins.py + the existing /tokens +
 * /backends + /operations + /artifacts surfaces used by Settings.
 */

import { api } from './client';

// ─────────────────────────────────────────────────────────────────
// Tokens
// ─────────────────────────────────────────────────────────────────

/**
 * Mirror of `media_engine/runtime/cache.py:ApiTokenInfo`. The server's
 * "revoked" signal is `revoked_at != None`, not a separate boolean —
 * we keep the raw shape over the wire and let the UI derive a flag.
 */
export type TokenInfo = {
  id: string;
  label: string;
  namespace: string;
  created_at: string;
  revoked_at: string | null;
};

export function isRevoked(t: TokenInfo): boolean {
  return t.revoked_at !== null;
}

export type TokenCreateResponse = {
  token_id: string;
  label: string;
  namespace: string;
  secret: string;
};

export function listTokens(includeRevoked = false): Promise<TokenInfo[]> {
  const qs = includeRevoked ? '?include_revoked=true' : '';
  return api.get<TokenInfo[]>(`/tokens${qs}`);
}

export function createToken(label: string, namespace: string): Promise<TokenCreateResponse> {
  return api.post<TokenCreateResponse>('/tokens', { label, namespace });
}

export function revokeToken(id: string): Promise<{ token_id: string; revoked: boolean }> {
  return api.delete<{ token_id: string; revoked: boolean }>(`/tokens/${encodeURIComponent(id)}`);
}

// ─────────────────────────────────────────────────────────────────
// Backends
// ─────────────────────────────────────────────────────────────────

export type BackendSummary = {
  op_name: string;
  name: string;
  version: string;
};

export type BackendDetail = BackendSummary & {
  requires: Record<string, unknown>;
  health: string;
};

export function listBackends(): Promise<BackendSummary[]> {
  return api.get<BackendSummary[]>('/backends');
}

export function backendDetail(name: string, op: string): Promise<BackendDetail> {
  return api.get<BackendDetail>(
    `/backends/${encodeURIComponent(name)}?op=${encodeURIComponent(op)}`,
  );
}

// ─────────────────────────────────────────────────────────────────
// Plugins — extras + catalog
// ─────────────────────────────────────────────────────────────────

export type ExtraRow = {
  name: string;
  packages: string[];
  installed: boolean;
  install_command: string;
};

export type ExtrasResponse = {
  items: ExtraRow[];
};

export function listExtras(): Promise<ExtrasResponse> {
  return api.get<ExtrasResponse>('/plugins/extras');
}

export type CatalogResponse = {
  ops: string[];
  backends: string[];
  hidden_ops: string[];
  hidden_backends: string[];
};

export function getCatalog(): Promise<CatalogResponse> {
  return api.get<CatalogResponse>('/plugins/catalog');
}

export function putCatalog(
  hidden_ops: string[],
  hidden_backends: string[],
): Promise<CatalogResponse> {
  return api.put<CatalogResponse>('/plugins/catalog', {
    hidden_ops,
    hidden_backends,
  });
}

// ─────────────────────────────────────────────────────────────────
// Storage
// ─────────────────────────────────────────────────────────────────

export type StorageStats = {
  permanent_store: string;
  workdir: string;
  /** Effective models cache directory (defaults to permanent_store/models).
   *  Settable via MEDIA_ENGINE_MODELS_DIR or config.toml. */
  models_dir: string;
  /** Free GiB on the volume that holds models_dir. Critical on Apple
   *  Silicon — a near-full internal SSD here is what causes MLX swap-
   *  thrash freezes. */
  models_free_gb: number;
  /** Effective HF_HOME the engine exported at boot (or operator-set). */
  hf_home: string;
  namespace: string;
  total_bytes: number;
  free_gb: number;
  by_kind: Record<string, { count: number; bytes: number }>;
};

export type GCResponse = {
  applied: boolean;
  workdirs_swept: number;
  workdir_candidates: string[];
  eviction_enabled: boolean;
  bytes_before: number;
  bytes_after: number;
  evicted_artifact_ids: string[];
};

export function storageStats(): Promise<StorageStats> {
  return api.get<StorageStats>('/storage/stats');
}

export function storageGC(
  apply: boolean,
  sweep_workdirs = true,
  evict = true,
): Promise<GCResponse> {
  return api.post<GCResponse>('/storage/gc', {
    apply,
    sweep_workdirs,
    evict,
  });
}

// ─────────────────────────────────────────────────────────────────
// Doctor — op→backend→requirements matrix (mirrors `med doctor --json`)
// ─────────────────────────────────────────────────────────────────

export type DoctorRequirement = {
  kind: 'env' | 'binary' | 'service' | 'hardware' | 'memory';
  name: string;
  status: 'ok' | 'missing' | 'degraded';
  detail: string;
};

export type DoctorBackend = {
  op_name: string;
  backend_name: string;
  backend_version: string;
  requirements: DoctorRequirement[];
  overall: 'ok' | 'degraded' | 'unavailable';
};

export type DoctorOp = {
  op_name: string;
  op_version: string;
  input_kinds: string[];
  output_kinds: string[];
  default_backend: string | null;
  has_router: boolean;
  embedded: boolean;
  backends: DoctorBackend[];
  overall: 'ok' | 'degraded' | 'unavailable';
  default_backend_status: 'ok' | 'degraded' | 'unavailable' | null;
};

export type DoctorReport = {
  summary: { ok: number; degraded: number; unavailable: number };
  ops: DoctorOp[];
};

export function getDoctor(opFilter?: string): Promise<DoctorReport> {
  const qs = opFilter ? `?op=${encodeURIComponent(opFilter)}` : '';
  return api.get<DoctorReport>(`/settings/doctor${qs}`);
}

// ─────────────────────────────────────────────────────────────────
// Secrets — known env-vars + write path
// ─────────────────────────────────────────────────────────────────

export type SecretInfo = {
  name: string;
  label: string;
  category: string;
  used_by: string;
  url: string;
  set: boolean;
  source: 'shell' | 'file' | 'unset';
  /** Ops that would have a working backend if this secret were set
   *  AND currently do not. */
  unblocks_direct: string[];
  /** Composites that transitively reach a directly-unblocked op via
   *  Operation.delegates_to (e.g. intelligence.summarize → extract). */
  unblocks_indirect: string[];
  /** Ops that already work via another backend; this secret would just
   *  add the cloud/extra alternative (not currently blocked). */
  adds_alternate: string[];
};

export type SecretsListResponse = {
  items: SecretInfo[];
  file_path: string;
};

export type SecretsUpdateResponse = {
  items: SecretInfo[];
  file_path: string;
  written: string[];
};

export function listSecrets(): Promise<SecretsListResponse> {
  return api.get<SecretsListResponse>('/settings/secrets');
}

/**
 * Apply a batch of secret-env updates. ``null`` (or empty string) deletes
 * the key. The server persists to `~/.config/media_engine/secrets.env`
 * (chmod 0600) and exports into the running process's env so backends
 * that read env at call-time see the change immediately. Backends that
 * snapshot env at import / boot still need a process restart — the UI
 * surfaces that caveat as a banner.
 */
export function putSecrets(updates: Record<string, string | null>): Promise<SecretsUpdateResponse> {
  return api.put<SecretsUpdateResponse>('/settings/secrets', { updates });
}

// ─────────────────────────────────────────────────────────────────
// Config files — read-only viewers
// ─────────────────────────────────────────────────────────────────

export type ConfigFileView = {
  path: string;
  exists: boolean;
  content: string;
  is_masked: boolean;
};

export type ConfigFilesResponse = {
  config_toml: ConfigFileView;
  resources_yaml: ConfigFileView;
  secrets_env: ConfigFileView;
};

export function getConfigFiles(): Promise<ConfigFilesResponse> {
  return api.get<ConfigFilesResponse>('/settings/config-files');
}

// ─────────────────────────────────────────────────────────────────
// Formatting helpers
// ─────────────────────────────────────────────────────────────────

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'] as const;

/**
 * Format a byte count with one decimal place per unit, scaling on
 * powers of 1024. Returns `"0 B"` for zero / negative inputs. Pure
 * function, easy to unit-test independent of any UI.
 */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 B';
  let i = 0;
  let value = n;
  while (value >= 1024 && i < UNITS.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${UNITS[i]}`;
}
