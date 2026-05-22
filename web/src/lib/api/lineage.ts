/**
 * LineageNode mirrors the `runtime.lineage.LineageNode` Pydantic shape
 * returned by `GET /artifacts/{id}/lineage?depth=N`.
 *
 * Each node represents one cached artifact + how it was produced.
 * The recursion bottoms out when the walker hits `max_depth` (in which
 * case `truncated_reason = "max_depth"`) or when an artifact has no
 * inputs.
 */

export type LineageNode = {
  artifact_id: string;
  kind: string;
  op?: string | null;
  backend?: string | null;
  truncated_reason?: 'max_depth' | 'cycle' | null;
  inputs?: LineageNode[];
};
