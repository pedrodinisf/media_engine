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
- `media_engine/web/` — built SvelteKit SPA (`dist/`) served by `med web
  start` at `/ui`. Source lives in top-level `web/` and is built via
  `bash scripts/build_web.sh` (or `pnpm -C web build`).
- `profiles/` — bundled starter profiles
- `infra/` — Dockerfile, docker-compose, helm, terraform skeletons
- `tests/` — Python unit + per-op + cross-validate
- `web/tests/` — frontend unit (`web/tests/unit/`) + Playwright e2e
  (`web/tests/e2e/`); see `web/playwright.config.ts`
- `docs/phase-6-5-bugs.md` — canonical bug ledger (IDs like `B-001`,
  `B-013` referenced in commits and verify scripts)
- `docs/web_ui_deferred.md` — Phase 6 v1 deferred items with bring-in
  triggers

## Common commands
- `uv sync` — install (core deps only). Backends live behind extras
  declared in `pyproject.toml` `[project.optional-dependencies]`
  (`transcribe-mlx`, `diarize`, `vlm-cloud`, `acquire-url`, `api`,
  `mcp`, `postgres`, …); install with `uv sync --extra <name>`.
  `med doctor` reports which extras are currently active per op.
- `uv run pytest -q` — Python unit + integration suite
- `pnpm -C web test:unit` — frontend unit (vitest under `web/tests/unit/`)
- `pnpm -C web test:e2e` — Playwright e2e (`web/tests/e2e/`); requires
  a built `media_engine/web/dist/` and is also driven by
  `scripts/verify_b001.sh` / `scripts/verify_settings.sh` against a
  live `med web start`
- `uv run pyright media_engine` — strict typecheck
- `uv run ruff check` / `uv run ruff format` — lint/format
- `uv run med ops` — list registered operations (35 as of Phase 6.7;
  `video.comprehend` is the most recent addition)
- `uv run med config` — print effective configuration
- `uv run med doctor [--op N] [--json]` — declarative dep map per op +
  backend. Walks every registered op, evaluates each backend's
  `BackendRequirements` against the live env (env vars, binaries,
  importable packages, hardware, RAM), prints a green/red matrix and
  exits non-zero if any op has no working backend. The answer to
  "what works right now on this machine?" Phase 6.5.
- `uv run python scripts/op_matrix.py [--filter X]` — runtime op
  matrix. Walks every op, attempts execution through `Engine.run`
  against synthetic fixtures, classifies as ✓/⊘/✗. Writes
  `tests/e2e_op_matrix_report.md`. Operator-invoked; complements
  `med doctor` by exercising the engine through real op paths.
- `bash scripts/verify_b001.sh [--headed]` — drive a real Chromium
  through the Job-detail SSE flow against a clean `med web start`.
  Regression gate for B-001 (SSE Events tab); operator-invoked.
- `bash scripts/verify_settings.sh [--headed]` — sibling of
  `verify_b001.sh`; regression gate for the Settings (Doctor +
  Secrets) + B-005 spec against a clean `med web start`.
- `bash scripts/verify_observability.sh [--headed]` — Phase 6.7
  regression gate for the Job-detail Logs tab + RAM/ETA gauges +
  the in-op `Progress` / `LogLine` SSE replay path. Boots a clean
  `med web start`, submits `video.extract_audio` on the bundled
  fixture, and asserts the Logs tab populates within 10 s. 3 specs.
- `bash scripts/build_web.sh` (or `pnpm -C web install && pnpm -C web
  build`) — rebuild the SvelteKit SPA into `media_engine/web/dist/`.
  CI / `hatch build` runs this first so the wheel ships the dist tree.
- `uv run med daemon start|status|stop` — warm-engine daemon lifecycle
- `uv run med profile ls|show|run` — discover / inspect / execute profiles
- `uv run med acquire <file>` — `acquire.upload` shortcut (local files)
- `uv run med acquire-url <url> [--quality] [--backend]` — `acquire.url`
- `uv run med acquire-live <url> [--max-duration N] [--segment-seconds N]
  [--hotkey "cmd+shift+j"]` — `acquire.livestream` recorder (SIGUSR1 splits)
- `uv run med extract-audio <video-id>` — `video.extract_audio` shortcut
- `uv run med run video.comprehend --input <video-id> --param fps=1.0
  --param synth_model=gemini-2.5-flash [--param style=lecture]
  [--param output_kind=structured|prose]` — Phase-6.7 composite: per-
  frame VLM + diarized transcript fused into one SOTA-LLM call. See
  `docs/phase-6-7.md` + `profiles/examples/video-comprehend.yaml`.
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
- `uv run med web start [--host] [--port] [--open]` — same boot as
  `med api start` plus mounts the built SvelteKit SPA at `/ui`. Primary
  GUI entrypoint; validates that `media_engine/web/dist/` exists. Use
  `--open` to auto-launch a browser at `/ui/setup`.
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

## Current state & roadmap

Phases are formalized in `~/.claude/plans/goofy-gathering-beaver.md`
§12.5 + §12.6 and must be respected by future planning.

**Shipped:**

- **Phase 6 — Local-first Web UI** *(shipped)*. SvelteKit SPA built
  into `media_engine/web/dist/` and served by `med web start` at
  `/ui`. Full GUI parity with the CLI: ingestion (upload / URL /
  livestream / batch), run configuration (op picker + auto-generated
  forms from `params_model.model_json_schema()` + cost preview +
  backend selector), job dashboard with SSE live updates, catalog
  browser, lineage graph viewer, search + cost ledger, profile
  workspace, plugin manager, settings (config.toml, resources.yaml,
  namespace switcher, token CRUD, backend health). Deferred v1 items
  are tracked in `docs/web_ui_deferred.md`; bug ledger lives in
  `docs/phase-6-5-bugs.md`.

- **Phase 6.6 — Audit + close-out** *(shipped — current version
  `0.6.2`)*. Closed every open Phase-6.5 bug: B-004 (locale-safe
  float input — `FloatInput.svelte` keeps a period-decimal text
  buffer so pt-PT users see `0.2` not `0,2`); B-006 (intelligence
  model dropdown via `json_schema_extra` + `_models.py` catalog);
  B-007 (composites forward `--backend` to delegates; precedence is
  explicit composite param > `ctx.backend` > delegate's router);
  B-008 (router model/backend consistency hard-fail at submit and
  `/run/preview`); B-009 (`med doctor` walks `delegates_to` and
  surfaces a per-delegate breakdown in `OpDoctorReport`
  + Settings → Doctor expand row, with cycle-guard). E2E specs
  in `scripts/verify_settings.sh` grew to 14 (was 11).

- **Hetzner deploy** *(shipped — `deploy/hetzner/`)*. One-shot
  Ubuntu provisioner that stands up the full stack (engine +
  Postgres/pgvector + Caddy + Let's Encrypt) on a single Hetzner
  Cloud VPS without modifying application code. Full operator guide
  at `docs/Hetzner_Deployment_Handbook.md` (14 sections: prereqs,
  one-shot deploy, day-2 ops, security model, sizing/swap, FAQ,
  disaster recovery, decommissioning). `Dockerfile.hetzner` extends
  the portable `infra/docker/Dockerfile` with the full extras + the
  Playwright two-step install; `docker-compose.override.yaml` adds
  Caddy and drops the public `:8000` bind so the engine is only
  reachable via the TLS-terminated reverse proxy.

- **Phase 6.7 — Live observability + `video.comprehend`**
  *(shipped — current version `0.7.0`)*. Two related shipments
  bundled into the same release because the second leans on the
  first for debugging UX:

  *Live observability* — every running op now emits a
  `Progress(phase="heartbeat", ...)` event every 2 s carrying
  available RAM, an ETA derived from `op.cost_estimate(...)`, and
  the model-pool byte estimate (`runtime/heartbeat.py`, wired in
  `runtime/engine.py`). The previously-defined-but-never-emitted
  `LogLine` event now flows: `runtime/log_pump.py` exposes
  `attach_subprocess()`, `attach_logger()`, and `attach_file_tail()`,
  and the load-bearing backends (ffmpeg in `extract_audio` +
  `sample_frames/ffmpeg_uniform`, `mlx-whisper`, `pyannote`,
  `vllm-mlx` server file-tail) all forward stdout/stderr or library
  loggers. Both `Progress` and `LogLine` now also carry `job_id`
  via two new `OperationContext` fields (`job_id`, `op_run_id`) so
  per-job SSE replay surfaces them on `/ui/jobs/[id]`. The Job-
  detail page grows a **Logs tab** (with per-source filter +
  auto-scroll + 2000-line dedicated buffer) and **live RAM/ETA
  gauges** in the status header. Operator-invoked regression gate
  at `bash scripts/verify_observability.sh` (3 specs).

  `video.comprehend` — new composite op. Fans out per-frame VLM
  calls at a user-chosen fps (vllm-mlx on Apple Silicon; cloud
  gemini on Linux), runs `audio.transcribe_diarized`, merges both
  timelines into a `MarkdownArtifact`, and feeds that to ONE SOTA
  LLM call (`intelligence.extract` for `output_kind=structured` /
  `intelligence.summarize` for `prose`). Hard-fails fast on
  fps × duration > max_frames (default 240) and on Linux + mlx
  vlm_model. The RAM-release helper from `transcribe_diarized`
  was renamed `release_audio_models` and now also drops
  `pyannote:*` slots from `ctx.model_pool`; the vllm-mlx backend
  exports a sibling `release_server(ctx)`. Default profile lives
  at `profiles/examples/video-comprehend.yaml`. Tests:
  `tests/test_op_video_comprehend.py` (10 unit specs covering
  fan-out, timeline merge, derived-id determinism, output_kind
  routing, hardware gate).

**Roadmap:**

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
