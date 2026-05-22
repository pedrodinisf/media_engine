# REST + MCP API Reference

`media_engine` exposes the same op + artifact surface through two
network protocols: a FastAPI HTTP server (for browsers / scripts / other
services) and an MCP stdio server (for LLM clients with tool-calling).
Both auto-expose ops registered in `bootstrap.py` — there is no separate
endpoint definition step when you add a new op.

The committed JSON schemas live next to this file:

- [`docs/openapi.json`](openapi.json) — full FastAPI OpenAPI 3.1 schema.
- [`docs/mcp_tools.json`](mcp_tools.json) — MCP tool definitions for the
  default (read-only) allow-list.

Refresh them with `python scripts/gen_openapi.py` and
`python scripts/gen_mcp_tools.py`.

## REST API

Boot it with:

```bash
uv run med api start --host 0.0.0.0 --port 8000   # headless
uv run med web start --open                       # same boot + /ui SPA mount
```

Auth: bearer tokens. Mint one with `med api token create --label <name>`;
pass it via `Authorization: Bearer <token>`. `/health` and `/ready` are
unauthenticated.

**Phase 6 SSE shim.** `EventSource` cannot set custom headers, so the
two SSE routes (`GET /jobs/{id}/events` and `GET /events/stream`)
accept the secret in the URL via `?token=...` as a fallback. Other
routes should keep using the `Authorization` header.

### Endpoint groups

| Group       | Routes                                                                                                  |
| ----------- | ------------------------------------------------------------------------------------------------------- |
| Liveness    | `GET /health` · `GET /ready`                                                                            |
| Run         | `POST /run` · `POST /run/preview` · `POST /pipelines`                                                   |
| Jobs        | `GET /jobs` · `GET /jobs/{id}` · `GET /jobs/{id}/events` (SSE) · `DELETE /jobs/{id}`                    |
| Events      | `GET /events/stream` (SSE) · `GET /events/history`                                                      |
| Acquire     | `POST /acquire/upload` · `POST /acquire/url/probe`                                                      |
| Search      | `POST /search`                                                                                          |
| Cost        | `GET /cost/summary` · `GET /cost/log`                                                                   |
| Artifacts   | `GET /artifacts` · `GET /artifacts/{id}` · `GET /artifacts/{id}/file` · `GET /artifacts/{id}/lineage`   |
| Profiles    | `GET /profiles` · `GET /profiles/{name}` · `POST /profiles`                                             |
| Operations  | `GET /operations` · `GET /operations/{name}`                                                            |
| Backends    | `GET /backends` · `GET /backends/{name}`                                                                |
| Tokens      | `POST /tokens` · `GET /tokens` · `DELETE /tokens/{id}`                                                  |

For every endpoint's request / response shapes, see `docs/openapi.json`
(committed; regenerate via `uv run python scripts/gen_openapi.py`) or
the auto-rendered Swagger UI at `/docs` when the server is running.

### Key endpoints in detail

#### `POST /run`
Request:
```json
{
  "op": "audio.transcribe",
  "inputs": ["a-3c1f..."],
  "params": { "model": "mlx-community/whisper-large-v3-mlx" },
  "backend": null
}
```
Response (202 Accepted):
```json
{ "job_id": "j-9b2c..." }
```
The op runs asynchronously. Subscribe to events at
`GET /jobs/{job_id}/events` (SSE: `OpStarted`, `Progress`, `LogLine`,
`OpCompleted`, `OpFailed`) or poll `GET /jobs/{job_id}`.

#### `POST /run/preview` (Phase 6 commit 42)
Same body shape as `POST /run`, returns a cost preview without
submitting a job. The Web UI's run panel debounces this on every
param change (250 ms). Response:
```json
{
  "op": "audio.transcribe",
  "backend": "mlx-whisper",
  "estimate_seconds_local": 12.0,
  "estimate_cost_cents": 0.0,
  "estimate_tokens_in": 0,
  "estimate_tokens_out": 0
}
```

#### `POST /pipelines`
Submit a profile by name (server-known) **OR** by inline YAML:
```json
{
  "profile_name": "analysis-full",
  "sources": [{"name": "source", "artifact_id": "v-91a3..."}]
}
```
Returns `202 { job_id }`. Inline YAML uses `"pipeline_yaml": "..."`
instead of `profile_name`.

#### `POST /acquire/upload` (Phase 6 commit 41)
Multipart upload + ffprobe preview / commit. Two modes:
- `commit=false` → streams the file to a tmp path, runs `ffprobe` +
  `classify`, returns `UploadPreview { kind, duration_s, codec, width,
  height, size_bytes, sha256_prefix }`.
- `commit=true` (default) → also submits `acquire.upload` and returns
  `JobAck { job_id }`.

Size cap honored: `MEDIA_ENGINE_MAX_UPLOAD_MB` (default 2048).
Bodies larger than the cap abort with `413` before reaching the
engine.

#### `POST /acquire/url/probe` (Phase 6 commit 41)
Runs `yt-dlp --dump-single-json` (no bytes downloaded) and returns:
```json
{
  "title": "…",
  "duration_s": 312.4,
  "uploader": "…",
  "thumbnail_url": "…",
  "formats_available": 9,
  "resolvable": true,
  "reason": null
}
```
`resolvable=false` + `reason="<hint>"` when `yt-dlp` is not on PATH
or the URL doesn't resolve.

#### `POST /search` (Phase 6 commit 46)
Synchronous catalog query — wraps `Engine.run("search.<mode>")` inline
so type-as-you-go feedback stays sub-second. No job, no SSE.
```json
{ "mode": "hybrid", "query": "speaker discussing logistics",
  "top_k": 10, "kind": null, "refresh": false }
```
Response:
```json
{
  "mode": "hybrid", "query": "...", "top_k": 10,
  "results": [
    {"artifact_id": "...", "kind": "transcript", "score": 0.78, "snippet": "..."}
  ]
}
```
- `top_k` is bounded `1..200` (plan §13 risk #6).
- 30 s timeout — long calls return `504` with a hint to use
  `POST /run` for batch.
- Semantic / hybrid modes embed the query string via
  sentence-transformers (`uv sync --extra embed`); the server
  returns `400` with an install hint if the extra is missing.

#### `GET /cost/summary` (Phase 6 commit 46)
Per-key spend rollup. Query params:
- `since` — ISO-8601 lower bound (optional).
- `until` — ISO-8601 upper bound (optional).
- `group_by` — `op` (default) · `backend` · `namespace`.

Response:
```json
{
  "rows": [
    {"key": "text.summarize", "count": 12, "total_cents": 22.5,
     "total_usd": 0.225, "tokens_in": 8192, "tokens_out": 1024}
  ],
  "total_cents": 22.5, "group_by": "op",
  "since": "...", "until": "..."
}
```

#### `GET /cost/log` (Phase 6 commit 46)
Paginated newest-first cost-log rows. Query params: `since`, `until`,
`op` (alias for `op_name`), `limit` (1..2000, default 200),
`offset` (≥0, default 0).
```json
{
  "items": [
    {"id": "...", "ts": "...", "op_name": "...", "backend_name": "...",
     "namespace": "default", "estimated_cents": 0.0, "actual_cents": 0.0,
     "tokens_in": 0, "tokens_out": 0, "duration_seconds": 0.2}
  ],
  "next_offset": 200, "limit": 200, "offset": 0
}
```
`next_offset` is `null` when the response is the last page.

#### `GET /events/stream` (Phase 6 commit 43)
Global SSE stream — every job's events on one socket. Accepts
`?token=...` for `EventSource` clients. Per-job consumers should
prefer `GET /jobs/{id}/events` (cheaper EventBus subscriber).

#### `GET /events/history` (Phase 6 commit 43)
Replay durable events from the `events` table. Query params:
`since`, `limit` (1..2000, default 200). Returns
`{ items: [...], limit }`.

#### `GET /artifacts?kind=transcript&limit=50&offset=...`
Paginated by `next_offset`. Pass it back as `offset=` to advance.

#### `GET /artifacts/{id}/file?token=...`
Binary stream of the artifact's underlying file. FastAPI's
`FileResponse` handles Range requests, so `<video controls>` and
`<audio controls>` can scrub natively. `?token=` is the SSE-style
fallback; non-browser clients should use the header instead.

#### `GET /artifacts/{id}/lineage?depth=10`
Returns the upstream tree as nested JSON. `depth` is bounded
`0..50`. Used by the catalog detail page's Lineage tab + the
standalone graph viewer (Svelte Flow + dagre).

### Operations introspection

`GET /operations` lists every registered op. `GET /operations/{name}`
returns the `params_model` JSON schema (`pydantic
BaseModel.model_json_schema()`) — the Web UI uses this to
auto-render parameter forms. Auto-derived `*_sha` fields are marked
`readOnly: true` so form generators (and MCP-driven LLMs) hide or
disable them.

### Errors

Standard HTTP error codes:

- `401 Unauthorized` — missing or invalid bearer.
- `404 Not Found` — unknown op/profile/artifact id.
- `409 Conflict` — content-addressed write race (rare).
- `422 Unprocessable Entity` — Pydantic validation failed on params.
- `500 Internal Server Error` — op raised an unhandled exception. The
  detail field includes the exception class.

Long-running op errors arrive on the SSE stream as a typed `error` event
rather than as a synchronous HTTP error.

## MCP (stdio)

Boot it with:

```bash
uv run med mcp serve              # read-only (default)
uv run med mcp serve --allow audio.transcribe --allow intelligence.summarize
```

`stdio` makes it directly mountable by LLM clients (Claude Code, the
MCP inspector, etc.). The server speaks the JSON-RPC 2.0 message
framing the spec defines.

### Tool surface

Every allow-listed op shows up as one MCP tool, named `media_engine.<op>`.
Its `inputSchema` is derived from the op's `params_model.model_json_schema()`,
with `inputs` (artifact ids) and `namespace` added as top-level fields.
The tool returns the structured op output as JSON.

The default allow-list ships read-only — only the three `search.*` ops
are exposed. To widen the surface, pass `--allow <op>` flags; to narrow
it, pass `--deny <op>` after an `--allow '*'`. The resolved policy is
printed to stderr on startup.

### Resources

The MCP server also exposes the artifact catalog through the
`resources/list` and `resources/read` methods (when supported by the
client). Resources are URI-addressed
(`media_engine://artifact/<id>`) and the same auth/namespace model
applies.

### Tool schema example

```json
{
  "name": "media_engine.search.hybrid",
  "description": "Hybrid (BM25 + embeddings) search over the catalog.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "inputs": { "type": "array", "items": {"type": "string"} },
      "namespace": { "type": "string", "default": "default" },
      "query": { "type": "string" },
      "top_k": { "type": "integer", "default": 10 },
      "kind": { "type": "string" }
    },
    "required": ["query"]
  }
}
```

See `docs/mcp_tools.json` for the full set (regenerate via
`python scripts/gen_mcp_tools.py`).

## Python API

For in-process use:

```python
from media_engine import Engine
from media_engine.config import EngineConfig
from media_engine.bootstrap import register_all

register_all()
with Engine.open(EngineConfig.load()) as engine:
    [transcript] = await engine.run(
        "audio.transcribe", inputs=["a-3c1f..."],
        model="mlx-community/whisper-large-v3-mlx",
    )
    print(transcript.metadata["text"])
```

The five-piece public API is re-exported from `media_engine.__init__`:
`Engine`, `Pipeline`, `Artifact`, `Kind`, `register_op`,
`register_backend`. Everything else is internal and may move between
0.x releases.

## Stability

Until v1.0 (which lands after Phase 6's Web UI), the REST + MCP surfaces
are **best-effort backwards-compatible** within a 0.x minor — the kind
of fields that round-trip through `docs/openapi.json` are stable, but
new fields can appear and minor enum widenings happen. Pin a specific
release if your integration is brittle to schema drift.
