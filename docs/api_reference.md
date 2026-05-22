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
uv run med api start --host 0.0.0.0 --port 8000
```

Auth: bearer tokens. Mint one with `med api token create --name <label>`;
pass it via `Authorization: Bearer <token>`. `/health` and `/ready` are
unauthenticated.

### Endpoint groups

| Group       | Routes                                                                                  |
| ----------- | --------------------------------------------------------------------------------------- |
| Liveness    | `GET /health` · `GET /ready`                                                            |
| Run         | `POST /run` · `POST /run/profile/{name}`                                                |
| Jobs        | `GET /jobs` · `GET /jobs/{id}` · `GET /jobs/{id}/events` (SSE) · `DELETE /jobs/{id}`    |
| Artifacts   | `GET /artifacts` · `GET /artifacts/{id}` · `GET /artifacts/{id}/file` · `GET /artifacts/{id}/lineage` |
| Profiles    | `GET /profiles` · `GET /profiles/{name}`                                                 |
| Operations  | `GET /operations` · `GET /operations/{name}`                                            |
| Backends    | `GET /backends` · `GET /backends/{name}`                                                 |
| Tokens      | `POST /tokens` · `GET /tokens` · `DELETE /tokens/{id}`                                   |
| Search      | (Use `POST /run` with `op=search.fulltext`/`semantic`/`hybrid`.)                         |
| Cost        | `POST /run` results carry usage; aggregate via the `med cost` CLI for now.               |

For every endpoint's request / response shapes, see `docs/openapi.json`
(committed) or the auto-rendered Swagger UI at `/docs` when the server is
running.

### Key endpoints in detail

#### `POST /run`
Request:
```json
{
  "op": "audio.transcribe",
  "inputs": ["a-3c1f..."],
  "params": { "model": "mlx-community/whisper-large-v3-mlx" },
  "backend": null,
  "namespace": "default"
}
```
Response (202 Accepted):
```json
{ "job_id": "j-9b2c...", "status": "queued" }
```
The op runs asynchronously. Subscribe to events at
`GET /jobs/{job_id}/events` (SSE: `progress`, `log_line`, `op_result`,
`error`, `done`) or poll `GET /jobs/{job_id}`.

#### `POST /run/profile/{name}`
Same shape, but `inputs` is mapped to the profile's declared `inputs`
slots. Convenient for the bundled `analysis-full` pipeline.

#### `GET /artifacts?kind=transcript&limit=50&cursor=...`
Paginated. The response includes a `next_cursor` field; pass it back as
`cursor=` to advance.

#### `GET /artifacts/{id}/lineage?depth=4`
Returns the upstream tree as nested JSON. Useful for "where did this
analysis come from" UIs.

### Operations introspection

`GET /operations` lists every registered op. `GET /operations/{name}`
returns the `params_model` JSON schema (`pydantic
BaseModel.model_json_schema()`) — Phase 6's Web UI uses this to
auto-render parameter forms.

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
