# CLAUDE.md — media_engine

## What this is
Universal media-processing engine. Typed artifacts, composable operations,
pluggable backends, content-addressed caching, async DAG execution. Ships as a
Python package + CLI (`med`) + daemon + REST + MCP.

## How to add a new operation
1. Pick a `<group>.<verb>` name (capability-based, not technology-based — e.g.
   `audio.transcribe`, never `mlx_whisper.transcribe`).
2. Create `media_engine/ops/<group>/<verb>.py`. Define a Pydantic `Params` model
   and an `Operation` subclass.
3. Implement `async run(inputs, params, ctx)` and `cost_estimate(inputs, params)`.
4. Multiple impls likely? Add a Backend layer under
   `media_engine/backends/<group>_<verb>/`.
5. `@register_op` in the same file.
6. Write `tests/test_op_<group>_<verb>.py`: success path, cache hit on rerun,
   param-change cache miss, error paths.
7. `uv run pytest -k <verb>` and `uv run pyright media_engine`.

## How to add a new backend
1. Pick the op (e.g. `audio.transcribe`).
2. Create `media_engine/backends/<group>_<verb>/<provider>.py`. Implement the
   `Backend` ABC.
3. Register via `BackendRegistry.register(YourBackend)`.
4. Declare `BackendRequirements` (env, binaries, services, hardware,
   `min_memory_gb`).
5. Test in `tests/test_backend_<group>_<verb>_<provider>.py`.

## How to write a profile
See `docs/writing_a_profile.md` (Phase 1). Two flavors: prompt (markdown with
YAML frontmatter) or pipeline (YAML DAG).

## Where things live
- `media_engine/artifacts/` — typed data
- `media_engine/ops/` — verbs (capability-named)
- `media_engine/backends/` — implementations (technology-named)
- `media_engine/runtime/` — `Engine`, cache, storage, DAG, server lifecycle,
  model pool, hardware, disk guard, GC, resources, ffprobe, lineage, cost
  tracker, retry, events, health
- `media_engine/profiles/` — profile loader + `Pipeline`
- `media_engine/cli/`, `daemon/`, `api/`, `mcp/` — adapters (transports)
- `profiles/` — bundled starter profiles
- `infra/` — Dockerfile, docker-compose, helm, terraform skeletons
- `tests/` — unit + per-op + cross-validate

## Common commands
- `uv sync` — install
- `uv run pytest -q` — all tests
- `uv run pyright media_engine` — typecheck
- `uv run ruff check` / `uv run ruff format` — lint/format
- `uv run med daemon start|status|stop` — daemon lifecycle (Phase 1+)
- `uv run med ops` — list registered operations
- `uv run med profile ls` — list discovered profiles
- `uv run med config` — print effective config
- `uv run med health` / `med ready` — operational checks (Phase 4+)

## Storage
- Permanent: `MEDIA_ENGINE_PERMANENT_STORE` (default
  `/Volumes/UNIVERSE_V/MEDIA/media_engine/`)
- Workdir: `/tmp/media_engine` (per-job; GC'd after 24 h on failure)
- Config: `~/.config/media_engine/config.toml` (or `MEDIA_ENGINE_*` env vars)
- Cache: `cache.db` (SQLite Phases 0–3; Postgres opt-in Phase 4+)

## Source projects (oracles, never replaced)
- `davos_video_grepper/` — WEF video intelligence; provides golden outputs for
  transcribe/diarize cross-validation.
- `framepulse/` — single-video studio; provides golden outputs for ffmpeg
  correctness, frame extraction, cost estimation patterns.

## Engine principles
1. Capability-named operations (not technology-named).
2. Content-addressed caching (artifacts have sha256 ids).
3. Backends are swappable — same op, different provider.
4. Profiles are data, not code (YAML or markdown with frontmatter).
5. The engine has zero domain opinions (no sentiment dims, no speaker schemas
   — those live in profiles).
6. Streaming-first: every op emits events.
7. MCP-native: every op auto-exposes as a tool.
8. DAG, not linear pipeline (fan-out, fan-in, parallelism within resource
   limits).
9. Cost-aware: `op.cost_estimate()` everywhere; `--dry-run` shows DAG total.
10. Resource-aware: declared resources → `asyncio.Semaphore` (e.g. 1 VLM at a
    time on Apple Silicon).
