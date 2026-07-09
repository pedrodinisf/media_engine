/**
 * Helpers + types for the JSON-Schema-driven form renderer.
 *
 * Pulled out into a plain .ts module so both the SchemaForm instance
 * script AND outside consumers (Vitest tests, callers building
 * initial values) can import without hitting Svelte's
 * module/instance-script boundary rules.
 */

export type FieldValue = string | number | boolean | null;
export type ParamsValue = Record<string, FieldValue>;

type AnyOf = { anyOf: SchemaNode[] };
export type SchemaNode = {
  type?: string;
  enum?: readonly (string | number)[];
  default?: unknown;
  title?: string;
  description?: string;
  readOnly?: boolean;
  format?: string;
} & Partial<AnyOf>;

export type ParamsSchema = {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, SchemaNode>;
  required?: readonly string[];
  $defs?: Record<string, SchemaNode>;
};

/** Pull the non-null branch out of a JSON Schema anyOf, carrying parent metadata. */
export function unwrapNullable(node: SchemaNode): { node: SchemaNode; nullable: boolean } {
  if (!node.anyOf) return { node, nullable: false };
  const non_null = node.anyOf.find((b) => b.type !== 'null');
  const has_null = node.anyOf.some((b) => b.type === 'null');
  if (!non_null) return { node, nullable: has_null };
  // exactOptionalPropertyTypes: only spread a field when the parent has
  // a value for it; otherwise leave it absent (vs. setting to undefined).
  const merged: SchemaNode = { ...non_null };
  if (node.title !== undefined) merged.title = node.title;
  if (node.description !== undefined) merged.description = node.description;
  if ('default' in node) merged.default = node.default;
  if (node.readOnly !== undefined) merged.readOnly = node.readOnly;
  if (node.format !== undefined) merged.format = node.format;
  return { node: merged, nullable: has_null };
}

/** Build a default-initialized params object from a schema. */
export function initialParams(schema: ParamsSchema): ParamsValue {
  const out: ParamsValue = {};
  for (const [name, raw] of Object.entries(schema.properties ?? {})) {
    const { node, nullable } = unwrapNullable(raw);
    if (node.readOnly) continue;
    if ('default' in raw && raw.default !== undefined) {
      out[name] = raw.default as FieldValue;
    } else if (nullable) {
      out[name] = null;
    } else if (node.type === 'boolean') {
      out[name] = false;
    } else if (node.type === 'number' || node.type === 'integer') {
      out[name] = 0;
    } else {
      out[name] = '';
    }
  }
  return out;
}

/** Heuristic — bumps fields with prompt/template/schema names to multiline. */
export function isMultilineField(name: string): boolean {
  return /prompt|system_prompt|schema_def|template/i.test(name);
}

export type ModelProvider = 'cloud' | 'local' | 'unknown';

/**
 * Classify a model id as cloud vs local by prefix.
 *
 * MIRRORS the server-side `classify_model_provider` in
 * `media_engine/profiles/introspect.py` (and the op routers'
 * `_backend_for_model`) — keep the two in sync. Same intent as how
 * `PROFILE_NAME_RE` mirrors the server's profile-name regex.
 */
export function classifyModelProvider(modelId: string): ModelProvider {
  if (/^(mlx-community|sentence-transformers|pyannote|BAAI)\//.test(modelId)) return 'local';
  if (/^(gemini-|claude-|gpt-|assemblyai\/)/.test(modelId)) return 'cloud';
  return 'unknown';
}

/** A field names a model when it is `model` or ends in `_model` (vlm_model,
 *  synth_model, transcribe_model, …). Mirrors the server `_MODEL_FIELD_RE`. */
export function isModelField(name: string): boolean {
  return /(^|_)model$/.test(name);
}
