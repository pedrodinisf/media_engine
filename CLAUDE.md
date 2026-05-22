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
2. Create the backend file under `media_engine/backends/`. Single-verb groups
   like `transcribe` or `diarize` go in `backends/<verb>/<provider>.py`;
   multi-verb groups go in `backends/<group>_<verb>/<provider>.py`
   (e.g. `frames_analyze/gemini.py`); group-only families like
   `backends/acquire/`, `backends/document/`, `backends/web/`,
   `backends/search/` keep the verb in the file name. Implement the
   `Backend` ABC.
3. Register via `BackendRegistry.register(YourBackend)` and add the class
   to `media_engine/bootstrap.py::_backend_classes()`. Optional-dep
   backends go in a `try/except ImportError` block and must be **import-
   clean** (lazy `importlib` inside the call path; the dep is only needed
   at `execute()` time, not registration time).
4. Declare `BackendRequirements` (env, binaries, services, hardware,
   `min_memory_gb`).
5. Test in `tests/test_backend_<descriptor>.py` (or fold into the op test
   when there's only one backend per op).

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
- `uv run pytest -q` — all tests (773 passing, 29 dep-gated skips)
- `uv run pyright media_engine` — strict typecheck
- `uv run ruff check` / `uv run ruff format` — lint/format
- `uv run med ops` — list registered operations (34 as of Phase 5)
- `uv run med config` — print effective configuration
- `uv run med daemon start|status|stop` — warm-engine daemon lifecycle
- `uv run med profile ls|show|run` — discover / inspect / execute profiles
- `uv run med acquire <file>` — `acquire.upload` shortcut (local files)
- `uv run med acquire-url <url> [--quality] [--backend]` — `acquire.url`
- `uv run med acquire-live <url> [--max-duration N] [--segment-seconds N]
  [--hotkey "cmd+shift+j"]` — `acquire.livestream` recorder (SIGUSR1 splits)
- `uv run med extract-audio <video-id>` — `video.extract_audio` shortcut
- `uv run med run <op> [--input ID] [--param K=V] [--backend B] [--schema P]`
  — generic single-op runner (cost preview, `--yes` to skip the prompt)
- `uv run med batch <file> [--op] [--input-arg] [--param]` — fan an op
  over a list of inputs through the DAG executor
- `uv run med search "<query>" [--mode fulltext|semantic|hybrid] [--top-k]
  [--kind] [--refresh]` — query the catalog
- `uv run med cost summary|ls` — actuals from `cost_log`
- `uv run med events tail|history` — engine event tail / history
- `uv run med lineage <id> [--depth N]` — render the upstream tree
- `uv run med mcp tools-json` — emit the MCP tool schema (per-op JSON)
- `uv run med mcp serve [--allow OP] [--deny OP]` — run the MCP stdio
  server (default policy: read-only — only `search.*` ops exposed)
- `uv run med api start [--host] [--port]` — boot the FastAPI REST surface
- `uv run med api token create|ls|revoke` — manage bearer tokens
- `uv run med db migrate [--db-url]` — alembic upgrade head against the
  configured cache (sqlite or postgres)
- `uv run med db dump-sqlite-to-postgres --to <url>` — one-shot SQLite →
  Postgres copy with pre/post sha256 verification
- `uv run med storage stats` — bytes-by-kind + free space
- `uv run med storage gc [--apply]` — workdir sweep + LRU eviction
  (eviction honored only when `eviction_enabled = true` in config)
- `uv run med storage migrate --from <a> --to <b>` — rewrite
  permanent_store path prefix in the cache (after moving files)
- `uv run med health` / `med ready` — operational checks (Phase 4+)

## Storage
- Permanent: `MEDIA_ENGINE_PERMANENT_STORE` (default
  `/Volumes/UNIVERSE_V/MEDIA/media_engine/`)
- Workdir: `/tmp/media_engine` (per-job; GC'd after 24 h on failure)
- Config: `~/.config/media_engine/config.toml` (or `MEDIA_ENGINE_*` env vars)
- Resources: `~/.config/media_engine/resources.yaml` (optional — overrides
  semaphore capacities / remaps which ops claim which resources)
- Cache: `cache.db` (SQLite Phases 0–3; Postgres opt-in Phase 4+)

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

## Roadmap (after Phase 5)

Two post-Phase-5 phases are formalized in
`~/.claude/plans/goofy-gathering-beaver.md` §12.5 + §12.6 and must be
respected by future planning:

- **Phase 6 — Local-first Web UI** (commits 39–50, ~3,500 LOC). A
  SvelteKit/Next.js SPA bundled in the engine container under `/ui`,
  served by `med web start`. Full GUI parity with the CLI — anything
  reachable via `med <verb>` is reachable from the UI. Scope:
  ingestion panel (upload / URL / livestream / batch), run
  configuration panel (op picker + auto-generated forms from
  `params_model.model_json_schema()` + cost preview + backend
  selector), job dashboard with SSE live updates, catalog browser
  with per-kind preview affordances, lineage graph viewer, search +
  cost ledger surfaces, profile workspace (visual DAG composer +
  YAML editor + examples library), plugin manager (toggle optional
  extras + custom op/backend modules), settings (config.toml,
  resources.yaml, namespace switcher, token CRUD, backend health).
  The shell stays first-class for power users and CI; the UI is
  the path of least resistance for everyone else.

- **Phase 7 — Acoustic speaker identity** (commits 51–54, ~1,500
  LOC). Extends Phase 5's name-DB `speakers.identify` with voice
  fingerprints. New ops: `speakers.embed_voice` (pyannote-embedding
  per Diarization turn), `speakers.cluster` (HDBSCAN cross-
  recording clustering → stable `Speaker_<sha8>` ids), `speakers.match`
  (cosine similarity vs a fingerprint DB, reusing
  pgvector/sqlite-vss). New artifact kinds: `SpeakerEmbedding`,
  `SpeakerProfile`. Privacy-by-default: namespace-scoped storage,
  per-namespace purge, opt-out for MCP/REST export. Same voice
  across different recordings gets the same stable id without
  needing a pre-built name database.

When adding new features or revisiting plans, check whether the work
fits Phase 5 / 6 / 7 scope before opening a new phase. Phase 5 stays
focused on domain profiles + reports + the existing name-match
`speakers.identify`; acoustic identity is explicitly Phase 7.
