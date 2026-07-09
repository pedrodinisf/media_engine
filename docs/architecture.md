# media_engine — Architecture

> Comprehensive reference for the engine as built (Phases 0–7 complete,
> commits 1–22 + audit hardening). The commit-by-commit roadmap and
> capability charter live in the implementation plan
> (`~/.claude/plans/goofy-gathering-beaver.md`); this document describes
> the system that exists today, every module, and *why* each design
> choice was made — including the ones that diverge from the plan.

---

## 1. What this is

`media_engine` factors media-processing capabilities into one substrate
so future applications are written as **profiles (data)**, not new
programs (code).

The substrate is five layers and four transports:

```
Transports   cli/  ·  daemon/  ·  api/ (REST + SSE)  ·  mcp/
                 \      |          |                 /
                  v     v          v                v
Engine        runtime/engine.py  -- runtime/dag.py (async DAG executor)
                  |
Ops           ops/<group>/<verb>.py        capability-named verbs
                  |  select_backend / ctx.backend
Backends      backends/<group>_<verb>/<provider>.py   swappable impls
                  |  read/write
Artifacts     artifacts/  typed, frozen, content-addressed (sha256)
                  |  persisted / indexed
Runtime infra storage.py · cache.py · events.py · cost_tracker.py ·
              gc.py · eviction.py · resources.py · health.py · ...
```

Every transport calls the same `Engine`. The Engine resolves an op +
backend, checks the content-addressed cache, and either returns the
cached artifact or runs the op (a backend produces a typed artifact),
persisting and indexing the result. Pipelines are the same thing fanned
out across a DAG.

---

## 2. Engine principles (and where they live)

1. **Capability-named operations.** `audio.transcribe`, never
   `mlx_whisper.transcribe`. Technology is a *backend*, not an op.
   Enforced by `ops/_registry.py` name validation.
2. **Content-addressed caching.** Every artifact id is a sha256; a
   *derived* artifact's id is the hash of `(kind, op, op_version,
   backend, backend_version, canonical_params, sorted(input_ids))`. Same
   inputs+params => same id => cache hit. `artifacts/base.py`,
   `runtime/cache.py`.
3. **Backends are swappable.** Same op, different provider. The
   `(backend.name, backend.version)` pair is in the cache key, so a
   Gemini transcript is never confused with an mlx-whisper one.
4. **Profiles are data, not code.** YAML pipelines / Markdown prompts;
   no domain logic in Python. `profiles/`.
5. **Zero domain opinions in core.** No sentiment dimensions, no speaker
   schemas. Those live in profile-supplied prompts and JSON schemas
   validated by `runtime/jsonschema.py`.
6. **Streaming-first.** Every op run emits `OpStarted` / `OpCompleted` /
   `OpFailed`; long ops emit `Progress`. `runtime/events.py`.
7. **MCP-native.** Every registered op auto-exports as an MCP tool.
   `mcp/exporter.py`.
8. **DAG, not linear pipeline.** Fan-out/fan-in with resource-bounded
   parallelism. `runtime/dag.py`.
9. **Cost-aware.** `op.cost_estimate()` everywhere; `--dry-run` and
   `med run` print a preview; actual spend is ledgered.
   `backends/_pricing.py`, `runtime/cost_tracker.py`.
10. **Resource-aware.** Declared resources -> `asyncio.Semaphore` (e.g.
    1 VLM at a time on Apple Silicon). `runtime/dag.py` semaphore pool.
11. **Feasibility declared by ops, surfaced before execution** (Phase 8).
    `Operation.validate_params(inputs, params)` (default no-op) raises a
    human message when a config can't succeed. `Engine.preview_pipeline`
    runs it + `cost_estimate` per node without executing, and it's surfaced
    everywhere: `POST /pipelines/preview` (the Web UI Run button blocks on
    it), `POST /run/preview`, and `med profile run --dry-run`. So
    `video.comprehend`'s `fps × duration > max_frames` fails at configure
    time, not after the run. `ops/_base.py`, `runtime/engine.py`.

---

## 3. The data model — artifacts

`artifacts/base.py` defines `Kind` (a `StrEnum`) and the frozen Pydantic
v2 `Artifact` base:

```python
class Artifact(BaseModel, frozen=True):
    id: str                    # sha256 — source: file bytes; derived: see below
    kind: Kind
    path: Path                 # {permanent_store}/artifacts/{sha[:2]}/{sha}.{ext}
    metadata: dict[str, Any]   # subclasses expose typed @property views
    derived_from: tuple[str, ...]
    produced_by: str | None    # op_run_id
    namespace: str = "default"
    created_at: datetime
```

Subclasses (one per `Kind`, in `artifacts/media.py`, `text.py`,
`analysis.py`) add read-only typed accessors over the untyped
`metadata` dict — kept flexible through Phase 3, will tighten to nested
sub-models once shapes stabilize.

**Kinds:** Video, Audio, Image, FrameSet, Transcript, Diarization,
OCRText, Chunks, Embedding, Analysis, SessionAnalysis, MarkdownArtifact,
Document, WebPage, SpeakerEmbedding, SpeakerProfile.

**Why content addressing:** the cache key *is* the identity. Re-running
an op with identical inputs/params is free and returns the byte-identical
artifact; bumping `op.version` or swapping a backend produces a new id
(old rows become unreachable, never corrupt). Hash determinism across
machines relies on canonical JSON (`sort_keys`) in
`canonical_params_hash`.

The public import surface (plan §4) is re-exported from
`media_engine/__init__.py`: `Engine, Pipeline, Artifact, AnyArtifact,
Kind, register_op, register_backend`.

---

## 4. Operations

`ops/_base.py` — the `Operation` ABC:

```python
class Operation(ABC):
    name: ClassVar[str]                 # "<group>.<verb>"
    version: ClassVar[str]              # semver; bump invalidates cache
    input_kinds: ClassVar[tuple[Kind, ...]]
    variadic_inputs: ClassVar[bool] = False
    output_kinds: ClassVar[tuple[Kind, ...]]
    params_model: ClassVar[type[BaseModel]]
    declared_resources: ClassVar[tuple[str, ...]] = ()
    default_backend: ClassVar[str | None] = None
    records_cost: ClassVar[bool] = True

    def select_backend(self, params) -> str | None: ...   # default None
    async def run(self, inputs, params, ctx) -> list[Artifact]: ...
    def cost_estimate(self, inputs, params) -> CostEstimate: ...
```

`ops/_registry.py` provides `@register_op` + `OpRegistry`; registration
is **explicit and idempotent** via `bootstrap.register_all()` (every
transport calls it at startup) rather than import-side-effect, so a test
that clears a registry can restore the production catalog with
`force=True`.

### 4.1 Input-kind validation & `variadic_inputs`

`Engine._validate_input_kinds` enforces `input_kinds` *positionally* by
default (N inputs, kind[i] must match exactly). Several ops take **one
input that may be one of several kinds** (`intelligence.*` accept
Transcript|Markdown|Analysis; `embed.text` accepts
Transcript|Markdown|Chunks) or **>=2 inputs each of a kind set**
(`frames.compare`). For these, `variadic_inputs = True` switches the
engine to **membership** validation (>=1 input, each in `input_kinds`);
the op enforces its own exact arity in `run()`.

> *Design note (audit fix):* `chunk.semantic` and `embed.text`
> originally under-declared `input_kinds` and hand-rolled `isinstance`
> checks, making charter-listed kinds unreachable through `Engine.run`.
> They now use `variadic_inputs` — one mechanism, three ops, reachable
> through every transport.

### 4.2 Backend selection (single source of truth)

An op with >=2 implementations has a Backend layer. The engine resolves
**one** backend name and that name is authoritative — it goes into the
cache key, `ctx.backend`, the cost ledger, and provenance. Precedence:

```
explicit backend=  >  op.select_backend(params)  >  default_backend
```

- `select_backend` lets an op pick by params (e.g. model prefix:
  `mlx-community/*` -> vllm-mlx/mlx-lm, `claude*` -> claude, else
  gemini).
- The op's `run()` dispatches with `BackendRegistry.get(self.name,
  ctx.backend)` — so the backend that *runs* is exactly the one
  *recorded*.

> *Design note (audit fix):* model-prefix ops previously self-selected
> the backend inside `run()` while the engine independently recorded
> `default_backend`, mislabelling provenance and cost. And
> `image.ocr/classify` exposed a `backend` *param* that collided with
> `Engine.run(backend=…)` (a `TypeError` through CLI/daemon/DAG, and the
> non-default backend was unreachable). Both are resolved by
> `select_backend` + `ctx.backend`; the colliding param was removed —
> backend is chosen the engine-standard way (`--backend` /
> `DAGNode.backend` / `RunOpRequest.backend`).

### 4.3 Composites

Thin wrappers delegate to a sub-op through `ctx.run_op` (the recursion
handle the engine injects): `audio.transcribe_diarized` ->
transcribe+diarize; `intelligence.summarize`/`classify` ->
`intelligence.extract` with a fixed schema. Composites set
`records_cost = False` so the wrapper's `Engine.run` does **not**
re-bill spend the delegated sub-op already ledgered (it returns the
sub-op's artifact, whose `usage` would otherwise be double-counted).

`intelligence.analyze` is a composite-by-backend: it slides a window
over a Transcript and calls the resolved extract backend's
non-persisting `extract_invoke` hook per window, finalizing each window
in-memory (`finalize_extract_data`).

> *Design note (audit fix):* analyze previously called
> `backend.execute` per window, which wrote one orphan content-addressed
> Analysis file per window into the permanent store (never registered,
> never GC-visible). The `extract_invoke` split removes persistence from
> the per-window path entirely.

### 4.4 Op catalog (Phases 0–7 complete, 38 ops)

| Group | Ops | Backend layer |
|---|---|---|
| acquire | upload, url, livestream | upload: — (local-fs) · url: yt-dlp/playwright-hls · livestream: ffmpeg-recorder |
| metadata | scrape_page | — (embedded playwright, lazy) |
| transcript | parse, merge | — (pure-Python; one parser for srt/speakered_txt/vtt) |
| document | parse | pymupdf (unstructured deferred) |
| web | fetch | httpx (static); playwright (render_js=True) |
| search | semantic, fulltext, hybrid | semantic: sqlite / pgvector · fulltext: sqlite-fts5 / postgres-tsvector · hybrid: composite (RRF) |
| video | extract_audio, trim, sample_frames, multimodal, comprehend | sample_frames: ffmpeg-uniform/pyscenedetect · multimodal: gemini/vllm-mlx · comprehend: composite |
| audio | transcribe, detect_language, diarize, transcribe_diarized | transcribe/detect: mlx-whisper · diarize: pyannote · t_d: composite |
| frames | subsample, analyze, compare | analyze: gemini/vllm-mlx · compare: gemini |
| image | describe, ocr, classify | describe: gemini · ocr: rapidocr/gemini-vision · classify: open-clip/gemini |
| chunk | semantic | default (nltk) |
| embed | text | sentence-transformers |
| intelligence | extract, summarize, classify, analyze | extract: mlx-lm/claude/gemini · others: composite |
| report | session, zeitgeist | — (pure-Python; profile-driven report renderers) |
| speakers | identify, embed_voice, cluster, match | identify: — (name-CSV fuzzy) · embed_voice: pyannote · cluster: hdbscan · match: sqlite / pgvector |

---

## 5. Backends

`backends/_base.py` — `Backend` ABC + `BackendRegistry` (keyed by
`(op_name, backend_name)`) + `BackendRequirements` (env / binaries /
services / hardware / min_memory_gb, surfaced by health checks) +
`retry_policy` classvar (per-backend override; `None` -> cloud/local
heuristic).

Backends are registered in `bootstrap._backend_classes()`. Optional-dep
backends (gemini, claude, mlx-lm, rapidocr, open-clip, pyannote,
sentence-transformers, pyscenedetect) are import-clean even when the ML
library is absent (lazy `importlib` inside the call path), and are
registered inside `try/except ImportError` — the dependency is only
needed at `execute()` time, not registration time.

Shared helpers: `backends/_pricing.py` (`MODEL_PRICING` tiered
≤200K/>200K input-token rates, longest-match `get_pricing`,
`estimate_cost_cents`, `estimate_video_tokens`); `backends/
_gemini_vision.py` (shared inline-image Gemini call for the vision ops).

The `intelligence.extract` backends additionally expose
`extract_invoke(source, params, ctx) -> (raw_text, usage)` — the
non-persisting hook (`ops/intelligence/extract.py:ExtractInvoker`
protocol) reused by `intelligence.analyze`.

---

## 6. Runtime subsystems (`runtime/`)

| Module | Responsibility |
|---|---|
| `engine.py` | Public API. `open_quick`/`open_session`; `run` (single op: cached, retried, evented, ledgered); `run_pipeline`; `estimate_pipeline_cost`; `cost_summary`/`cost_log_entries`/`event_log_entries`; read surface (`get_artifact`, `list_artifacts`, `lineage`, `resolve_id`). |
| `cache.py` | SQLAlchemy 2.0 (SQLite + WAL pragmas; Postgres in Phase 4). Tables: `cached_artifacts`, `cached_operation_runs` (unique on the cache-key tuple), `cost_log` (append-only spend ledger), `events` (durable event tail), `jobs` (REST submissions, Phase 4 commit 29), `api_tokens` (bearer-token hashes, Phase 4 commit 29). `to_orm`/`to_pydantic` are the only Pydantic<->ORM crossings. |
| `storage.py` | `StorageBackend` Protocol + `LocalFSStorage`: atomic `.tmp`+rename, sha256 2-char sharding, hardlink mode, per-job workdir. |
| `dag.py` | `Pipeline`/`DAGNode` dataclasses; topological wave sort (cycle/unknown-dep detection); `asyncio.TaskGroup` per ready-wave; per-resource `asyncio.Semaphore` pool; per-node retry; **partial completion** (a failed node fails its dependents as `FailedDependency`, siblings still run). |
| `retry.py` | `RetryPolicy` (exponential/fixed backoff + jitter); `classify_exception` -> (retryable, retry_after, error_class, suggested_action); `with_retry` (honors server `Retry-After`, skips deterministic/auth, propagates cancellation); `policy_for(backend_name)` shared by DAG + single-op paths. |
| `events.py` | Event types (`OpStarted/Progress/ArtifactReady/OpCompleted/OpFailed/LogLine`); in-process `EventBus` (bounded per-subscriber queues + synchronous sinks); `build_op_failed` (structured envelope from the same classifier the retry layer uses). |
| `cost_tracker.py` | `CostTracker` over the `cost_log` ledger: per-op rollup + recent-runs; `parse_since`. |
| `lineage.py` | `LineageNode` tree walked from `derived_from` through the cache. |
| `model_pool.py` | HF-cache-aware warm model pool; `get_or_load(key, loader)` (race-tolerant). |
| `server_manager.py` | Process-backed backend lifecycle (start/stop/health/wait), pid files under `server-state/`. Used by vllm-mlx. |
| `hardware.py` | Memory-fit checks before loading a model. |
| `disk_guard.py` | `assert_free_space` precondition (refuse to start a writing op below `MEDIA_ENGINE_MIN_FREE_GB`); skipped on cache hits. |
| `ffprobe.py` | `probe()` + `classify()` (codec -> Kind) for `acquire.upload`. |
| `jsonschema.py` | Zero-dependency JSON-Schema validator (type/required/properties/additionalProperties/items/enum/min-max). Keeps the core dependency list lean; lenient (unknown keywords ignored). |
| `gc.py` | Workdir garbage collection (Phase 4 commit 32). `sweep_workdirs` drops subdirs older than the retention window; `periodic_workdir_gc` is the async loop the daemon spawns at startup (interval from `MEDIA_ENGINE_GC_INTERVAL`). |
| `eviction.py` | Opt-in LRU eviction (Phase 4 commit 32). Walks the cache oldest-first and deletes non-protected artifacts (files + cache rows + dependent operation_runs) until total bytes fit under `eviction_max_gb`. Protected kinds (Video/Audio by default) are never evicted. Dry-run mode reports what *would* happen. |
| `resources.py` | `resources.yaml` loader (Phase 4 commit 34). Loads `{config_dir}/resources.yaml` to override semaphore capacities and remap which ops claim which resource; the original `declared_resources` is snapshotted so a later config that drops the op restores its default. |
| `health.py` | Liveness + readiness probes (Phase 4 commit 33). `liveness()` is unconditional; `readiness()` walks (storage writable, cache reachable, daemon socket present) and returns a structured `HealthReport`. |

### 6.1 The `Engine.run` lifecycle

```
resolve op -> resolve inputs -> validate kinds -> build params model
-> resolve backend (explicit > select_backend > default)
-> cache lookup (op,ver,backend,ver,params_hash,inputs,ns)
   |_ hit -> return cached artifacts (no disk gate, no events, no ledger)
-> disk-space gate -> workdir
-> op.cost_estimate(...)      (PRE-run — Phase 6.7 — heartbeat needs ETA)
-> ctx (backend, run_op, emit, pools, job_id, op_run_id)
-> emit OpStarted
-> spawn heartbeat task        (Phase 6.7 — emits Progress every 2 s)
-> with_retry(op.run)         (policy: backend.retry_policy or by-name)
   |_ failure -> emit OpFailed(envelope) -> raise
-> cancel + await heartbeat    (finally block)
-> record_run (cache row, idempotent on the key tuple) — reuses the
   pre-run cost
-> record_cost (cost_log) — skipped when op.records_cost is False
-> emit OpCompleted
-> stamp produced_by, upsert artifacts, return
```

Events fan out to subscribers (daemon stream) and to a synchronous
persistence sink that writes the `events` table; the Engine prunes
events older than 7 days on open (best-effort, swallowed on error).
The `op.cost_estimate(...)` call is *pre-run* since Phase 6.7 because
the heartbeat task needs the local-seconds estimate to compute a
running ETA from the moment `OpStarted` fires; the same estimate
feeds the post-run cache + cost-ledger writes.

### 6.2 OperationContext

What an op receives: `workdir`, `config`, `storage`, `namespace`,
`emit`, `server_manager`, `model_pool`, `run_op` (composite recursion
handle), `backend` (engine-resolved name — the dispatch source of
truth), `cache` (read-only handle, set by `Engine.run`, used by
index-building ops like `search.*` to enumerate persisted artifacts
across runs — `None` outside Engine.run), and **`job_id` /
`op_run_id`** (Phase 6.7 — the submission id + per-run id the engine
assigned, forwarded onto every `Progress` / `LogLine` a backend
emits so per-job SSE replay surfaces them in the Web UI). Resource
semaphores are acquired by the DAG executor *around* the op, so ops
stay declarative and never touch a lock.

---

## 7. Profiles

`profiles/schema.py` — Pydantic `PipelineProfile` (YAML DAG) and
`PromptProfile` (Markdown + frontmatter), both carrying
`profile_schema_version`. `profiles/loader.py` discovers profiles in
`{config_dir}/profiles/`, `<repo>/profiles/`, and `--profile-dir`,
validating that ops/backends exist and the graph is acyclic.
`profiles/pipeline.py` compiles a profile into the `runtime/dag.Pipeline`
the executor consumes.

> *Design note:* `Pipeline`/`DAGNode` live in `runtime/dag.py` (the
> executor owns its data shape); `profiles/pipeline.py` is the compiler.
> The plan §5 placed `Pipeline` under `profiles/` — the implemented
> split is cleaner and is reflected in the public re-export.

> *Design note (audit fix):* `PromptProfile`'s output-schema field is
> `schema_path` (Pydantic reserves `schema`); it now carries
> `alias="schema"` so the documented frontmatter key actually populates
> it.

---

## 8. Transports

- **CLI (`cli/`)** — Typer. `med acquire/extract-audio/ls/show/lineage/
  ops/config`; `med run <op>` (generic: cost preview -> confirm unless
  `--yes`; global `--dry-run` prints & exits; `--input/--backend/
  --param k=v/--schema`); `med profile ls|show|run`; `med batch`;
  `med acquire-live` (live HLS capture; SIGUSR1 / pynput-hotkey
  segmentation); `med search` (`--mode fulltext|semantic|hybrid`,
  `--kind`, `--top-k`, `--refresh`); `med cost ls|summary`; `med events tail|history`; `med daemon|mcp`.
  `cli/_handle.py` is the daemon-aware seam: a 50 ms ping picks a
  daemon-routed handle or falls back to in-process `open_quick` — command
  code never branches on "is the daemon up?".
- **Daemon (`daemon/`)** — asyncio JSON-line frames over a Unix socket
  wrapping a warm `Engine.open_session()`. `protocol.py` carries
  `protocol_version`; the client negotiates on connect.
  `subscribe_events` keeps the connection open and pushes
  `EventNotification` frames. Pipelines run client-side dispatching each
  node's op through the daemon (warm models, no `RunPipeline` RPC
  needed — both sides share one `cache.db` + store).
- **REST (`api/`)** — FastAPI app (Phase 4 commit 29; Phase 6 commits
  39–46 widen the surface). Endpoints:
  `POST /run`, `POST /run/preview`, `POST /pipelines` (the first two
  return Pydantic responses; `/pipelines` + `/run` return 202 +
  `job_id`), `GET/DELETE /jobs[/id]`, `GET /jobs/{id}/events` (SSE
  through `sse-starlette`), `GET /events/stream` (global SSE) +
  `GET /events/history` (persisted tail),
  `POST /acquire/upload` (multipart, ffprobe preview + commit) +
  `POST /acquire/url/probe` (yt-dlp `--dump-single-json`),
  `POST /search` (sync wrapper around `search.*` ops with a 30 s
  timeout), `GET /cost/summary` + `GET /cost/log` (read-side views
  over the `cost_log` table),
  `GET /artifacts[/id][/file][/lineage]`,
  `GET/POST /profiles`, `GET /operations[/name]`,
  `GET /backends[/name]`, `POST/GET/DELETE /tokens`, plus
  un-authenticated `/health` + `/ready` probes. Bearer-token auth
  (32-byte secrets hashed at rest in `api_tokens`); the token's
  namespace scopes every read/write. The SSE routes accept
  `?token=...` as a fallback because `EventSource` cannot set
  custom headers. `med api start` boots uvicorn against
  `build_app()`; `med api token create` mints the first bootstrap
  token directly against the cache (no chicken-and-egg).
- **MCP (`mcp/`)** — `exporter.py` turns every registered op into an MCP
  tool (`.`->`__`, params schema + `input_artifact_ids` + `backend`
  enum; variadic ops get an unbounded `minItems:1` array).
  `server.py` (Phase 4 commit 30) is the stdio server: `tools/list`
  filtered by `MCPSecurityConfig` (default = read-only `search.*`),
  `tools/call` dispatches to `Engine.run`, `resources/list` +
  `resources/read` surface artifacts as `media://<kind>/<id>`. `med
  mcp serve [--allow OP] [--deny OP]` is what
  `claude mcp add media-engine "med mcp serve"` invokes.
- **Web UI (`web/` source; `media_engine/web/dist/` built)** —
  SvelteKit 2 / Svelte 5 SPA bundled into the engine container at
  `/ui`, served by the same FastAPI process as REST (Phase 6
  commits 39–50). Static-only `adapter-static` build (no Node in
  production), Tailwind v4 design tokens lifted from
  `docs/quickstart.html` (Clean-NASA palette), TypeScript strict
  + `exactOptionalPropertyTypes` + `noUncheckedIndexedAccess`,
  `EventSource` + TanStack-Query-style fetch helpers, schema-driven
  form renderer over `params_schema`, Svelte Flow + dagre for the
  lineage graph. The build output is force-included in the wheel
  via `hatch.build.targets.wheel.force-include`, so `pip install
  media_engine[api]` from PyPI ships the UI without a Node
  toolchain. `med web start` is a thin wrapper over `med api start`
  that validates the dist tree is present and (optionally) opens
  a browser at `/ui/setup`. CSP scoped to `/ui/*` via
  `media_engine/api/middleware.py`. See
  [`web_ui.md`](web_ui.md) for the panel-by-panel tour.

---

## 9. Caching, cost, retry — exact semantics

- **Cache hit** requires the full key tuple to match *and* every output
  artifact to still exist on disk; a row pointing at a missing artifact
  falls through and re-runs (lazy stale-row GC).
- **Cost** is two-stage: upfront `op.cost_estimate` (summed across a DAG
  by `estimate_pipeline_cost`; source-fed cached nodes contribute 0,
  downstream nodes priced with empty inputs since their inputs are
  unknown pre-run — a *preview*, not a guarantee) and post-hoc actuals
  from backend-reported `usage` written to `cost_log` (one row per real
  execution; cache hits and `records_cost=False` composites are not
  billed).
- **Retry** is identical on the single-op and DAG paths
  (`retry.policy_for`): cloud-tagged backend names get 3 attempts with
  exponential backoff, local 1; a backend may override via
  `retry_policy`; 429/transient retried (honoring `Retry-After`),
  auth/deterministic not, cancellation propagated immediately.

### 9.1 Reanalysis recipe (cache-hit math by example)

Content addressing makes "the same job again, but with one knob
tweaked" cheap by construction. Walk the bundled
`profiles/examples/url-to-summary.yaml` four-node DAG:

```
video      = acquire.url(url="…", quality="best")
audio      = video.extract_audio(video)
transcript = audio.transcribe(audio,  language="en")
summary    = intelligence.summarize(transcript, focus="main argument")
```

**First run.** No cache hits — every node fetches its inputs, runs,
writes a `cached_operation_runs` row, persists the artifact, and a
`cost_log` row. The total spend is the sum of all four.

**Second run, summarize focus changed to `"counterarguments"`.** The
derived id of `summary` is `sha256(kind="analysis", op="intelligence.summarize",
op_version, backend, backend_version, canonical_params={focus:…},
sorted(input_ids=[transcript.id]))` — the params dict changed, so
the id changes. But the *upstream* nodes' params are identical, the
upstream input ids are identical, the upstream op versions are
identical → their derived ids are unchanged → they each hit the
cache. `med cost summary` shows a single new row (the summarize call);
the other three contribute 0.

**Force a fresh fetch upstream.** Two knobs:

1. **Bump `op.version`** on the upstream op (in source). Every derived
   id downstream of that op recomputes. This is the right move when
   the op's *semantics* changed (e.g., transcribe now emits
   word-level timestamps).
2. **Pass a `refresh_nonce`** param (currently honored by `search.*`)
   or change an input param. This is the right move when the *outside
   world* changed (the page at that URL was updated; the live stream
   was re-recorded) but the op itself didn't.

The same recipe works for the larger bundled `analysis-full` pipeline
profile and for any user-authored pipeline — the math is identical,
just with more nodes.

`med lineage <id>` renders the upstream tree end-to-end (depth-limited
+ cycle-safe + tagged with `truncated_reason` when a branch is
elided). `--json` returns the full structure for REST consumers in
Phase 4.

---

## 10. Module map (one line each)

```
media_engine/
├── __init__.py            public re-exports (Engine, Pipeline, Artifact, …)
├── bootstrap.py           register_all() — the op + backend catalog
├── config.py              pydantic-settings, MEDIA_ENGINE_* env, config.toml
├── logging_setup.py       text default, JSON via MEDIA_ENGINE_LOG_FORMAT
├── artifacts/             base (Kind/Artifact/hashing) · media · text · analysis
├── ops/                   _base · _registry · <group>/<verb>.py (38 ops)
├── backends/              _base · _pricing · _gemini_vision · <group>_<verb>/<provider>.py
├── runtime/               engine · cache · storage · dag · retry · events
│                          cost_tracker · lineage · model_pool · server_manager
│                          hardware · disk_guard · gc · eviction · resources
│                          ffprobe · jsonschema · health
├── profiles/              schema · loader · pipeline
├── cli/                   __init__(entry) · _handle · run/cost/events/profile/
│                          daemon/mcp/batch · acquire_live · search
│                          api · db · storage · health
├── daemon/                protocol · server · client · entry
├── api/                   app · routes · auth · jobs · sse · health · _state
├── mcp/                   exporter · server
└── _alembic/              env.py + versions/0001_initial_schema (ships in the wheel)

alembic.ini                repo-root convenience for ``alembic upgrade head``
infra/                     docker (Dockerfile + compose) · helm · terraform
```

---

## 11. Status & deviations from the plan

**Phases 0–5 complete** (commits 1–38 + audit-fix commits per phase).
Phase 4 added the FastAPI REST surface + `Job` concept + bearer-token
auth (commit 29); the full MCP stdio server with read-only-by-default
allow-list (commit 30); the Postgres / pgvector / postgres-tsvector
backends + alembic migrations + `med db migrate|dump-sqlite-to-postgres`
(commit 31); LRU eviction + workdir GC + `med storage stats|gc|migrate`
(commit 32); the IaaC package (Dockerfile, docker-compose, Helm chart,
Terraform module), `/health` + `/ready` probes + `med health|ready`,
and `docs/deployment.md` (commit 33); and the `resources.yaml` loader
(commit 34). **Phase 5 (commits 35–38)** added `speakers.identify`
(rapidfuzz name-CSV fuzzy match, commit 35); the bundled `analysis-full`
pipeline profile + `AnalyzeParams.prompt_path` resolution (commit 36);
`report.session` + `report.zeitgeist` Jinja2 renderers + the five
starter `kind: prompt` profiles (commit 37); and the v0.5.0 release —
README rewrite, `cli_reference.md`, `api_reference.md`,
`adding_a_backend.md`, committed `openapi.json` + `mcp_tools.json`, the
e2e demo script, and `CHANGELOG.md` (commit 38). A second pre-Phase-6
audit pass (post-release) tightened three suboptimal patterns: the
FastAPI app's `version` now sources `media_engine.__version__` (was
hardcoded `0.1.0`); the `speaker_extra` payload in `speakers.identify`
moved out of a quadratic nested generator into an O(N+M) lookup
helper; and `IdentifyParams.speaker_db` / `SessionReportParams.template`
/ `ZeitgeistReportParams.template` are now `Field(exclude=True)` so the
content-addressed cache keys depend on the file's sha (via the
auto-derived `*_sha` fields), not on the filesystem path — two
callers referencing the same file by different paths now hit the
cache. A third audit pass focused on Phase 4 (commits 29–34) caught
one production-blocker, two real bugs, and seven robustness
improvements: the Dockerfile referenced a nonexistent root `alembic/`
directory (the migrations actually ship inside the package); the
`cancel_job` endpoint could overwrite a terminal status with
`cancelled` under a race; `runtime/eviction.py` deleted
`cached_operation_runs` rows without a namespace filter (now applied
as defense-in-depth). Polish: SQLite search payloads gained the
`"backend"` field for parity with Postgres; bearer parsing tolerates
extra whitespace; the readiness storage check actually writes+deletes
a probe file rather than trusting `os.access`; readiness now gates on
`min_free_gb`; the daemon GC loop logs sweep failures instead of
swallowing them; `resources.yaml` rejects unknown keys (typo
detection); `med storage migrate` validates `--to` exists before
rewriting; the SSE pumper is awaited on disconnect so the
`bus.subscribe()` generator's cleanup runs deterministically. A
final integration validation pass marked the auto-derived `*_sha`
fields (``IdentifyParams.speaker_db_sha``,
``SessionReportParams.template_sha``,
``ZeitgeistReportParams.template_sha``) as ``readOnly`` in their
JSON schemas with a description so MCP-driven LLMs and the
forthcoming Phase 6 Web UI form generator hide or disable them
instead of treating them as settable strings — the validator
already overwrote any client value, so this is purely a UX
clarification; the regenerated ``docs/openapi.json`` +
``docs/mcp_tools.json`` reflect both markers.
**38 ops.** Suite: 1116 passed / 27 skipped (dependency/API-key/network
gated); `ruff` and strict `pyright` clean. Frontend: 70 Vitest unit
tests, svelte-check 0/0 on 582 files.

> *Charter deviation (commit 27).* The plan §3 names the semantic
> backend ``sqlite-vss`` (loadable extension). We ship a plain SQLite
> + brute-force cosine implementation as backend ``sqlite`` — same
> storage schema, no optional dep, sub-1k-artifact corpora stay snappy.
> An ``sqlite-vss`` backend can land later as a *separate* backend
> name (cache keys are backend-versioned, so swapping is non-breaking).

Reasonable, intentional divergences from the plan text (the plan is the
roadmap; this section is the reconciliation):

- `OperationContext` shape differs from plan §2.6 (no `artifact_path`/
  `acquire_resource`/`select_backend` callables; has `storage`,
  `run_op`, `backend`) — ops stay declarative; resource locks are the
  DAG executor's job.
- `Pipeline` lives in `runtime/dag.py`, not `profiles/pipeline.py`
  (executor owns its data shape; profiles compile into it).
- No `RunPipelineRequest` daemon RPC — pipelines dispatch per-node
  through the daemon over the shared cache instead (documented design
  in `cli/_handle.py`).
- `ExtractParams.schema_def` (not `schema`) — avoids shadowing
  Pydantic's `BaseModel.schema`.
- Removed by "best part is no part": no `cost.estimate_dag` op
  (`Engine.estimate_pipeline_cost`), no `acquire.batch` op (`med
  batch`).
- New mechanisms not in the original plan, added because the
  capability charter required them: `Operation.variadic_inputs`,
  `Operation.select_backend` + `ctx.backend`, `Operation.records_cost`,
  the `extract_invoke` non-persisting hook, `runtime/jsonschema.py`,
  `OperationContext.cache` (Phase 3 — read-only handle for index-
  building ops like `search.*`), and `LineageNode.truncated_reason`
  (surfaces *why* a lineage walk stopped — `"max_depth"` today;
  `"cycle"` reserved for a future case content addressing makes
  impossible).
- Phase-3 charter reshapings: `search.semantic` accepts only an
  `Embedding` input (the charter's `str|Embedding` is split across
  layers — the CLI / `search.hybrid` embed the string and feed the
  resulting id to the op); `search.*` outputs an `Analysis` wrapping
  `{results: [...], …}` rather than a bare `list[Artifact+score]`
  (engine ops must return `list[AnyArtifact]`); `acquire.livestream`
  ships one `ffmpeg-recorder` backend that internally calls the
  `playwright-hls` sniff (charter "playwright-hls + ffmpeg-recorder"
  read as pipeline, not two backends); `document.parse` ships the
  nullary path only — `unstructured` and the `Document→Document`
  re-process path land when a profile actually consumes them;
  `pgvector` / `postgres-tsvector` move to Phase 4 with the Postgres
  migration.
- Phase-4 ratified deviations:
  - **`pgvector` + `postgres-tsvector` land as *separate* backend
    names** alongside `sqlite` / `sqlite-fts5`, not replacements. Plan
    §11 commit 31 reads "pgvector replaces sqlite-vss when Postgres is
    the cache"; the engine's cache key embeds `(backend_name,
    backend_version)`, so swapping is non-breaking only if the names
    differ. Existing SQLite-cached results stay reachable.
  - **MCP default allow-list is `{search.semantic, search.fulltext,
    search.hybrid}`.** Plan §11 commit 30 reads "default = read-only
    ops — search/ls/show/lineage". `ls`/`show`/`lineage` are
    CLI/REST verbs, not registered ops; the equivalent read-only
    access is `resources/list` + `resources/read`, which are part of
    the MCP protocol regardless of the op allow-list. So the
    op-allow-list defaults to the searchable family; `tools/list`
    only exposes those, while `resources/list` always works.
  - **Configurable cache URL accepts two env aliases.** Plan §11 +
    docker-compose + Helm use `MEDIA_ENGINE_DB_URL`; the Pydantic
    field is `cache_db_url` (env: `MEDIA_ENGINE_CACHE_DB_URL`). A
    `validation_alias` accepts both so either spelling works.
  - **`med storage migrate` rewrites cache rows, not config.** Plan
    §11 commit 32 says "atomic path update"; we read it as the
    cache-side rewrite. Operators move the files themselves
    (`rsync`/`mv`) and then run the command to fix every artifact
    `path` column. The engine's `permanent_store` config is a
    separate edit.
  - **Namespacing isolates the cache, not the on-disk layout.** Plan
    §11 commit 32 sketches `{permanent_store}/namespaces/{ns}/
    artifacts/`. The cache already enforces full isolation
    (`namespace` is in every row's primary-key tuple), so on-disk
    sharding is operator clarity only — deferred until an explicit
    need lands.
  - **`med api token create|ls|revoke`** (not `list`) — matches the
    `cost ls` / `events tail` convention already established for
    every other `med <group>` subcommand.
  - **`Operation.declared_resources` is mutable at runtime** when
    `resources.yaml` remaps an op. The snapshot of the compile-time
    tuple is kept in `runtime/resources.py` so a later config that
    drops the op restores its original claim — repeated applies are
    idempotent.
  - **REST does not route through the daemon.** Plan §11 commit 29
    text says "if daemon up, REST routes through it". The
    implemented model is one-or-the-other: each transport boots its
    own warm engine. They share the same `cache.db` + permanent_store
    (content-addressed, so reads cross transports cleanly), so
    artifacts produced through one appear in the other immediately.
    Running both is fine but redundant — production picks the
    transport that fits.
  - **MCP `notifications/progress` is deferred.** `tools/call`
    completes synchronously today; threading the engine's `EventBus`
    through the MCP request context is left until an actual MCP
    consumer asks for it.
  - **MCP `resources/read` returns inline JSON for every kind** (the
    plan mentions "signed URL or inline if small"). Signed URLs
    presume an HTTP backend reachable from the MCP client; today the
    stdio transport carries JSON payloads only, and clients fetch
    binary bytes through `GET /artifacts/{id}/file` over REST.
  - **`med api token create` writes only the secret to stdout** so
    the plan's gate (`TOKEN=$(med api token create)`) works
    straight. Context (id, namespace, label, "save it now" notice)
    goes to stderr; `--json` retains the structured form.
  - **Alembic migrations ship inside the package** as
    `media_engine/_alembic/`. The wheel only packages
    `media_engine/`, so leaving the migrations at the repo root
    would have broken `med db migrate` on installed wheels.
    `cli/db.py:_alembic_config` builds the alembic ``Config``
    in-process and pins ``script_location`` at the packaged
    directory; `alembic.ini` at the repo root remains for direct
    `alembic upgrade head` invocations and points at the same path.
  - **`med db migrate --db-url X` honors X.** `_alembic_config`
    stamps `config.attributes['url_source'] = 'cli'`; the in-package
    `env.py` skips the env-driven override when it sees that flag.
  - **`Engine.run` stamps the engine's namespace on outputs.** Ops
    construct artifacts with the Pydantic field default
    (`namespace="default"`); the engine is the single place that
    owns the namespace decision per call. The `produced_by`
    finalize loop now passes `namespace=self.config.namespace`
    alongside `produced_by`, and `Engine.run_pipeline` (plus the
    daemon-routed handle) stamps `pipeline.sources` the same way so
    the inner `Engine.run` dispatches can resolve them.
  - **Token namespace must match the engine's.** `require_token`
    returns 403 when a token's namespace doesn't equal
    `state.engine.config.namespace` — the engine is single-namespace
    per process, and silently writing to one namespace while reads
    filter by another would only confuse callers. Multi-tenant
    deployments run one API process per namespace.
  - **`cache.upsert_artifact` raises `ValueError` on namespace
    conflict.** Same `id` under a different namespace would
    otherwise surface as a deferred SQL `IntegrityError`; we now
    catch it at the SQLAlchemy boundary with a clear message. The
    underlying schema has `id` as the primary key, so cross-tenant
    content sharing is not supported in v1.
  - **MCP server is import-clean without the SDK installed.**
    `media_engine/mcp/server.py` lazy-imports `mcp.types` and
    `mcp.server.Server` inside `build_mcp_server` so the rest of
    the CLI keeps working on a base install (no `mcp` extra).
  - **API lifespan owns workdir GC** alongside the daemon's. An
    API-only deployment (no daemon container) still sweeps orphan
    workdirs on the same configurable interval as the daemon does.
  - **`POST /profiles` validates the profile name** against a strict
    kebab-case pattern + a path-relative-to check so a malicious
    `name` like `../../etc/passwd` returns 400 rather than writing
    outside the profiles directory.
  - **Helm chart ConfigMap shipped** alongside the Deployment /
    Service / Ingress / Secret / PVC templates. Non-secret env vars
    (`MEDIA_ENGINE_PERMANENT_STORE`, `LOG_FORMAT`, `MIN_FREE_GB`,
    plus `config.extraEnv`) flow through the ConfigMap via
    `envFrom` on the Deployment.
  - **Terraform module exposes a `cluster` variable** for downstream
    callers to tag the release with a cluster identifier; passes
    through to the chart as `MEDIA_ENGINE_CLUSTER_LABEL` (pure
    metadata, no behavior change).
  - **`cache.prune_events` is namespace-scoped.** Engine startup
    prune used to delete every event row older than the cutoff
    across all namespaces; the call now passes
    ``self.config.namespace`` so a multi-tenant deployment
    (one API process per namespace sharing one cache.db) keeps
    each tenant's event tail intact.
  - **MCP `_call_tool` defensively pops `inputs`** so a client that
    smuggles it into `arguments` (alongside the schema-declared
    `input_artifact_ids`) doesn't crash with
    `TypeError: got multiple values for keyword argument 'inputs'`.
  - **API startup recovers orphaned jobs.** New
    `Cache.fail_orphaned_jobs` sweeps `running`/`pending` rows on
    lifespan boot and flips them to `failed` with an
    `InterruptedRun` envelope so clients see a terminal state
    after a crash instead of a permanently in-flight row.
- Phase-5 ratified deviations:
  - **`speakers.identify` operates on `Transcript → Transcript`**, not
    `Diarization → Diarization` as the plan text said. The as-built
    `Diarization` artifact carries only `{speaker_id, start, end}`
    per segment — text lives in the `Transcript` that
    `audio.transcribe_diarized` emits (with `speaker_id` stamped on
    each segment by `_align_speakers`). The op consumes that
    Transcript and emits a Transcript carrying a
    `speaker_name` per segment + a top-level `speaker_names` map +
    `speaker_match_meta` (confidence scores). Cluster ids are
    preserved, lossless. The output kind matches the input — the
    plan's `Diarization → Diarization` framing was a description
    error, not an architectural change.
  - **`IdentifyParams.speaker_db_sha` is auto-derived**. A
    `model_validator(mode="before")` hashes the CSV bytes into a
    16-char prefix that participates in canonical params, so editing
    the file invalidates the cache. The `speaker_db` Path stays for
    user clarity but isn't load-bearing on the cache key.
  - **`AnalyzeParams.prompt_path` is dict-input only.** A
    `model_validator(mode="before")` reads the file's text and
    inlines it into `prompt`; the path is popped from the data dict
    so it never enters canonical params. Cache key tracks resolved
    text, not file path — editing the `.md` invalidates cache on the
    next run.
  - **`intelligence.analyze` bumped to 1.1.0.** Two additive changes
    that the bundled `analysis-full` profile needed: (1) pass
    `speaker_names` through from the input Transcript's metadata to
    the output SessionAnalysis's metadata, so `report.session` can
    render a Speakers section; (2) read each window's speaker from
    `speaker_name` first, then `speaker_id`, then `speaker` — picks
    up `speakers.identify` resolutions and the cluster ids that
    `audio.transcribe_diarized` stamps. Output shape only grew;
    bumping version (per engine principle 2) makes the change
    discoverable through the cache.
  - **`SessionReportParams.template_sha` + `ZeitgeistReportParams.template_sha`
    auto-derived** via the same `model_validator(mode="before")`
    trick. Editing a `.j2` template invalidates the cache without
    requiring a version bump on the op.
  - **`report.session` and `report.zeitgeist` use lenient Jinja2
    `Undefined`.** A missing analysis key renders as empty string
    rather than aborting the whole report. Markdown is already a
    forgiving format; tight strictness would make a single bad
    window kill a 50-window render.
  - **The bundled-profile discovery path was already wired** in
    `cli/profile.py:27` (`repo_dir = Path(__file__).parents[2] /
    "profiles"`). Phase 5 commit 35's plan text reads "add
    <repo>/profiles/ to default discovery paths"; we read that as
    populating the directory, since the discovery loader (and the
    CLI it's wired into) already accepts the path.
  - **The bundled `analysis-full` schema uses generic dimensions**
    (`summary`, `topics`, `entities`, `claims`,
    `sentiment{polarity,confidence}`, `questions`). The engine's
    zero-domain principle (`§3`) extends to the bundled profiles —
    they need to be reusable across content domains. Specialize by
    cloning the directory and rewriting the schema + prompt; no
    engine change required.
  - **`profiles/*.md` paths are resolved against CWD,** not the
    profile file's parent. Today's pipeline compiler doesn't pass a
    profile-dir context through to param resolution; users run
    `med profile run analysis-full` from the repo root for now. A
    profile-dir-relative resolver is a small Phase 6 enhancement
    (the Web UI will need it for non-CWD workflows anyway).
- Phase-6 ratified deviations (commits 39–48 + post-46 + post-48 audits):
  - **Framework choice: SvelteKit, not Next.js.** Plan §12.5 said
    "SvelteKit/Next.js"; the smaller bundle + `adapter-static`'s
    "no Node in production" model decided it. Build emits to
    `media_engine/web/dist/` and FastAPI mounts it as
    `StaticFiles(html=True)` at `/ui`. Per `web_ui.md` §7.
  - **Paste-token bootstrap, not a login UI.** Plan §12.5 lists
    "auth bootstrap" without prescribing a form. The engine is
    local-first + single-namespace-per-process — bearer tokens
    minted via `med api token create` already are the user
    identity. The UI's `/ui/setup` route just persists the
    pasted secret; zero new endpoints, zero chicken-and-egg.
  - **SSE auth rides on `?token=...`** for `EventSource` clients
    (`GET /jobs/{id}/events` + `GET /events/stream`). Plan §13.1
    pre-registered the tradeoff: tokens in URL query strings
    leak via browser history and access logs, but the v1 target
    is loopback / private networks. Hardening path (job-scoped
    short-lived nonce) catalogued in `web_ui_deferred.md`.
  - **`POST /search` is sync, not a job.** Search ops are pure
    reads and finish in 100–500 ms on sub-1k corpora; wrapping
    them in `POST /run` would add 1–2 s of job-lifecycle latency
    and break the type-as-you-go UX. The endpoint calls
    `Engine.run("search.<mode>")` inline under a 30 s timeout.
    `top_k` bounded at 200 to keep the synchronous handler from
    starving the event loop (plan §13 risk #6).
  - **Layer Chart used for d3-scale-driven HTML bars** rather
    than full SVG `Chart` components. The `CostBars.svelte`
    rollup is a sequence of `<div>` percentage bars sized via
    `d3-scale`'s `scaleLinear`. The chart library is in place
    for richer visuals in commits 47–49; the simpler HTML
    rendering compiles cleanly under Svelte 5 strict mode and
    needs no SVG layer manipulation for the bar use case.
  - **Datetime windows use the `datetime-local` input with a
    local↔UTC bridge.** `<input type="datetime-local">` rejects
    `Z`-suffixed ISO values and emits local-time-without-tz on
    edit. `web/src/lib/api/cost.ts` exports
    `isoToLocalInputValue` + `localInputValueToIso` so state is
    canonical UTC ISO over the wire while the input displays
    the user's wall-clock time. Labels marked "(local)" to set
    expectations. Three Vitest unit tests cover the round-trip.
  - **`/cost/log` until-filter applied in-route, not in cache.**
    `Engine.cost_log_entries` has no upper bound. When `until`
    is set, the route fetches *unbounded* rows (since the
    bounded fetch would shrink the candidate set before the
    filter ran), filters in Python, then paginates. Acceptable
    at local scale; if cost-log size becomes a bottleneck, push
    `until` into `Cache.cost_log`. Regression covered.
  - **Query-side embedding lives in `runtime/search_query.py`.**
    `search.semantic` and `search.hybrid` accept only an
    `Embedding` artifact as input — both `med search` and
    `POST /search` need the same upstream "encode query, persist
    as Embedding, return its id" step. Centralising avoided the
    two transports drifting on model choice. The op_name passed
    into `compute_derived_artifact_id` stays `_cli.search.query`
    verbatim so the refactor doesn't invalidate any cached
    query-embedding rows; the leading `_cli.` reads odd outside
    the CLI but is now an opaque cache-key seed.
  - **Search results FE shape.** The endpoint flattens the
    `Analysis` wrapper (charter §11 deviation note about search
    ops emitting an Analysis wrapping `results: [...]`) into a
    bare ranked list — the Web UI never has to know that
    `search.semantic` emits an Analysis artifact, only that
    the endpoint returns ranked rows. CLI behaviour unchanged.
  - **YAML is the canonical source of truth in the profile
    workspace.** Commits 47+48 give the composer a parsed view of
    the YAML for visual rendering, but every edit mutates the
    `yaml.Document` AST and re-serializes — comments + key order
    on the rest of the file survive byte-identical. The composer
    is a *view*; the YAML pane is the *model*.
  - **`POST /profiles/validate` parses YAML in memory** (post-
    commit-48 audit) via the new `load_profile_from_string`
    helper. The first cut wrote a tmp file per request to reuse
    the path-based loader's error messages, but with the
    workspace firing validate every 500 ms idle that became a
    measurable hotspot (5 syscalls/call: mkdir + write + read +
    unlink + rmdir). The string loader feeds errors the same
    way using a `source` label argument in place of the path.
  - **Per-node SchemaForm editing in the workspace is deferred.**
    Commit 47 ships the split-view with id + backend editing per
    selected node; the full schema-driven param form (already
    in production at `/ui/run`) is queued as a small follow-up.
    For v1, users edit params directly in the YAML pane —
    every save round-trips through the `Document` AST so
    comments + key order survive.
  - **`ProfileSummary.source` field** (post-commit-48 audit)
    replaces the FE `/config/`-substring heuristic with a
    server-supplied `"bundled" | "user"` discriminator. The
    pre-audit heuristic was wrong for non-default config dirs;
    the field is computed via `resolved.is_relative_to(user_dir)`
    inside `list_profiles_endpoint`.
  - **YAML-driven rename is rejected at save time** (post-
    commit-48 audit). Editing the top-level `name:` key and
    saving would have created a NEW profile file alongside the
    original. The workspace's `save()` now compares
    `parsed.name` against the route name and refuses divergence,
    pointing the user at the explicit fork-then-delete path.
  - **Composer layout debounced 150 ms** (post-commit-48 audit).
    The YAML editor pushes `yamlText` updates on every keystroke;
    the parsed graph + dagre layout consume a separate
    `yamlForLayout` $state that lags by 150 ms. Single canvas
    repaint per typing pause instead of per keystroke. Validate
    debounces a further 500 ms on top of that.
  - **Plugin catalog gate is enforcement-only, not security**
    (commit 49). `plugins.toml` hides ops/backends from discovery
    surfaces (REST `/operations`, MCP `tools/list`, Web UI op
    picker) but they stay registered. The MCP allow-list
    (`MCPSecurityConfig`) remains the boundary for "this client
    cannot call this op"; the catalog is "this op shouldn't show
    up in my UI" for tidying.
  - **Extras catalog hard-coded in `api/plugins.py`** (commit 49)
    rather than parsed from `pyproject.toml` at request time.
    The wheel ships without `pyproject.toml`, so a runtime parse
    would 500 on a PyPI install. A regression test asserts
    bidirectional parity at CI time.
  - **`declared_resources` carried on `OperationSummary`** (post-
    commit-49 audit), not just `OperationDetail`. The Web UI's
    Settings → Config tab needs the field for every op; pre-audit
    it fired 1 list + N detail requests just to read this one
    string list. Lifting the field is cheap (a few tens of bytes
    per row) and saves N round-trips per tab activation.
  - **`/storage/*` reuses `state.engine.cache`** (post-commit-49
    audit) instead of constructing a fresh `Cache(...)` per
    request. SQLAlchemy spins up a new engine + pool per
    construction; reusing the long-lived cache the engine already
    holds removes that per-click overhead.
  - **`+ui` Dockerfile is a Node-free runtime image** (commit 50).
    The build adds a `node:22-bookworm-slim` stage that runs
    `pnpm -C web install --frozen-lockfile && pnpm -C web build`
    against the `web/` source tree; the runtime stage stays on
    `python:${PYTHON_VERSION}-slim` and only `COPY --from=ui-build`s
    the populated `media_engine/web/dist/` directory. No Node
    binary, no pnpm cache, no JS toolchain leaks into the runtime
    image. `tests/test_dockerfile.py` asserts the `ui-build` stage
    is present and the runtime stage does not `apt-get install
    nodejs` to keep that invariant.
  - **`docs/quickstart.html` Web UI + Profiles expansion** (commit 50)
    folds the panel-by-panel tour from `web_ui.md` + the profile
    authoring loop from `writing_a_profile.md` into the executive
    overview, so the single HTML doc is self-contained for a
    new reader. Additive only — nothing was removed.
  - **CSP `script-src` allows `'unsafe-inline'`** (commit 50,
    discovered while running the screenshot generator). SvelteKit's
    adapter-static index.html ships an inline boot ``<script>`` that
    bootstraps the SPA (`__sveltekit_<hash> = { base, assets };
    Promise.all([…])`). The earlier `script-src 'self'
    'wasm-unsafe-eval'` blocked it; the SPA never hydrated and any
    browser hit got a blank page. Without runtime nonces (which a
    static mount can't supply) the choice is between accepting
    `'unsafe-inline'` (matches `style-src`'s existing posture) or
    rotating hashes per build (brittle). v1 takes the former; the
    httpOnly-cookie + hash-rotation hardening path is catalogued
    in `web_ui_deferred.md`.
  - **`GET /ui/{spa_path:path}` SPA-fallback handler** (commit 50,
    same discovery). `StaticFiles(html=True)` only serves index.html
    for the directory root; a browser refresh on `/ui/jobs` or any
    direct deep-URL paste hit FastAPI and got `{"detail":"Not
    Found"}`. The fallback handler returns the real asset when the
    path resolves to a file under the dist tree (`/ui/_app/...`,
    `/ui/favicon.ico`) and otherwise returns `index.html` so
    SvelteKit's client router takes over. Path-traversal guarded
    via `.resolve().relative_to(dist.resolve())`. Four regression
    tests in `tests/test_api_ui_mount.py` cover the deep-path,
    asset-passthrough, and traversal cases.

Audit-driven correctness fixes are called out inline as *Design note
(audit fix)*. Reconciliation commits: `fix(phase-1): close audit
findings`, `fix(phase-2): close pre-Phase-3 audit findings`,
`fix(phase-3): close pre-Phase-4 audit findings`, `fix(phase-4): close
pre-Phase-5 audit findings`, `fix(phase-5): close pre-Phase-6 audit
findings`, `fix(phase-6): post-commit-46 audit — until-pagination,
datetime-local tz, cost-page race`, `fix(phase-6): post-commit-48
audit — validate string-loader, composer debounce, source field,
rename guard`, `fix(phase-6): post-commit-49 audit — cache reuse,
N+1 collapse, drift guard`.

Phases 0–7 are complete; v0.8.0 is the current release. Phase 6
(local-first Web UI) closed with commits 39–50 + three audit-fix
passes (post-46, post-48, post-49) + two docs syncs in the
release window. Commit 50 shipped the docs refresh, the six
bundled screenshots (regenerable via
`scripts/gen_ui_screenshots.sh`), the `+ui` multi-stage Dockerfile
(Node-free runtime image), and the `docs/quickstart.html` Web UI +
Profiles expansion.

**Phase 6.5 — Quality + UX bug triage (v0.6.1, 2026-05-23).**
A short post-release pass kicked off by manual smoke testing of
v0.6.0. The session decision (deliberately scoped down from a
prior plan that called for a full 7-phase cross-transport
regression suite) was: **build introspection surfaces for the
op→backend→dep contract that already existed in code but was
never reachable to operators, then fix the highest-blast-radius
bugs the surfaces turn up.** Four commits:

- **`med doctor`** (`runtime/doctor.py` + `cli/doctor.py`,
  ~300 LOC) — walks every registered op + backend, evaluates each
  ``BackendRequirements`` against the live env (env vars, binaries
  on PATH, importable Python packages, hardware tag, RAM), prints
  a green/red matrix, exits non-zero if any op has no working
  backend. ``--op X`` filters; ``--json`` machine-readable.
  Surfaces the dep contract the engine had silently in code but
  never told operators about (the user's repro: form rendered fine,
  cost preview computed fine, submit silently failed deep in
  ``Engine.run``). Non-router ops roll up to the *default backend's*
  status (a working alternative doesn't help if the default route
  is broken); router ops keep "ok if any" semantics, with the deep
  view flagging when the default route is the broken one.

- **Op matrix runner** (`scripts/op_matrix.py`, ~540 LOC) —
  walks every op, attempts execution through ``Engine.run`` against
  synthetic vendored fixtures (one per Kind), classifies as
  ✓ / ⊘ / ✗. Runtime failures matching known dep-gap patterns
  ("X is not installed", "API key", etc.) are reclassified as ⊘ so
  ✗ truly means engine bug. Pre-filters via doctor; uses the doctor
  report to steer through an alternate backend when the static
  default is broken but the router has a working alternative.
  Output: `tests/e2e_op_matrix_report.md`. Current result: ✓ 14
  · ⊘ 20 · ✗ 0.

- **B-001 — Job-detail SSE Events tab** (p0). Three independent
  root causes contributing to "Events tab spins on `Waiting for
  events…` forever": (1) `Engine.run` minted its own internal id
  and stamped events with it, so the REST `job_id` never matched
  the SSE filter — `Engine.run` now accepts an optional ``job_id``
  kwarg that the REST `_run_single_op` / `_run_pipeline` propagate;
  `ctx.run_op` is wrapped in a closure that pre-fills the parent's
  `job_id` so composite sub-op events stay correlated. (2) The Web
  UI SSE wrapper listened for ``OpStarted/...`` (PascalCase) but
  server emits ``op_started/...`` (snake_case from `Event.type`) —
  every named-event frame was dropped on the floor; fixed both
  sides. (3) Even with (1) and (2) fixed, a client subscribing
  AFTER the event has fired (the common race for fast ops) missed
  it — `EventBus.subscribe()` only delivers events emitted after
  registration. Added replay-on-subscribe to `api/sse.py`: query
  the persistent `events` table by `job_id`, yield any persisted
  frames first, then switch to live mode with `event_id` dedup
  against the replayed set.
  - New schema: `events.job_id` column + `idx_events_job` index.
    Alembic migration 0002 (additive, nullable, sqlite + postgres
    compatible).
  - `EventLogEntry.id` now uses `Event.event_id` as the PK so
    replay/live dedup is trivial.
  - Regression coverage: 3 new `tests/test_api.py` tests +
    `web/tests/e2e/flows/sse_events.spec.ts` Playwright spec
    against a real Chromium against a live `med web start`.
    Runner: `scripts/verify_b001.sh`.

- **B-003 — `med api token create --namespace`** (p1) defaulted to
  literal ``"default"`` instead of reading `MEDIA_ENGINE_NAMESPACE`,
  so tokens minted on a non-default engine ns 403'd on every authed
  endpoint. `cmd_token_create` now reads `EngineConfig().namespace`
  when `--namespace` is unset.

- **B-010 — Under-declared backend deps** — `transcribe/mlx_whisper`,
  `document/pymupdf`, `embed_text/sentence_transformers` had
  `BackendRequirements()` without their `services=[…]` Python-package
  entries. Doctor reported them green even when missing; users hit
  opaque runtime errors. Declarations updated alongside the doctor
  rollout.

- **Triaged bug log** (`docs/phase-6-5-bugs.md`) — 10 surfaced
  bugs (now 7 open: 1 p1, 6 p2) with reproducible steps, suspected
  causes, and current status. Future sessions resume from here.

**Upgrade note:** v0.6.0 → v0.6.1 introduces a new SQLite/Postgres
column (`events.job_id`). Sqlite users on a Cache-created DB get
the column automatically via `Base.metadata.create_all`; users
upgrading an existing deployment must run `med db migrate` to apply
0002_events_job_id. SSE replay silently degrades to live-only mode
on a pre-migration DB (the live stream still works because the
client-side snake_case fix is schema-independent).

The last formalised phase (plan §12.6), now shipped:

- **Phase 7 — Acoustic speaker identity** *(shipped — v0.8.0)*. Voice
  fingerprinting on top of `audio.diarize`. New ops:
  `speakers.embed_voice`, `speakers.cluster` (HDBSCAN cross-
  recording), `speakers.match` (cosine vs a fingerprint DB, sqlite +
  pgvector backends). New artifact kinds: `SpeakerEmbedding`,
  `SpeakerProfile`. Stable `Speaker_<sha8>` ids that re-identify the
  same voice across recordings without a pre-built name DB, reconciled
  against saved profiles via a running-mean centroid. Privacy-by-
  default: opt-in namespace-scoped storage, per-namespace purge,
  MCP hidden + REST opt-out. See `docs/phase-7.md`.

---

## 12. Live observability (Phase 6.7)

`Engine.run` carries a per-invocation **heartbeat task** that wakes
on a fixed interval (default 2 s) and emits a
`Progress(phase="heartbeat", …)` event carrying three new optional
fields:

* `available_memory_gb` — `psutil.virtual_memory().available / GiB`
  at tick time. Drives the Web UI's RAM-free gauge.
* `eta_seconds` — `cost_estimate.local_seconds − elapsed`, floored
  at zero. Drives the ETA countdown.
* `pool_bytes_estimate` — `ctx.model_pool.total_bytes_estimate()`,
  so operators can spot a model-pool blowup.

The heartbeat is `asyncio.create_task`'d after `OpStarted` and
cancelled+awaited in the same `finally` block that already cleaned
up the workdir. Production cost is ~0.1 ms per tick; `EventBus.emit`
drops the oldest queue entry on a full subscriber so slow clients
never wedge the producer.

The previously-defined-but-never-emitted `LogLine` event now flows
through `media_engine/runtime/log_pump.py`:

| Surface             | Use                                        |
|---------------------|--------------------------------------------|
| `LinePump.push`     | Public form of the dedup/cap state machine; backends that already iterate stdout/stderr inline (`extract_audio` parses `time=` for Progress) push lines through it without spawning a second pump. |
| `attach_subprocess` | Async `Process` stdout + stderr → LogLine per line. ffmpeg in `sample_frames/ffmpeg_uniform.py` uses this. |
| `attach_logger`     | Bridges a stdlib `logging` logger → LogLine. `mlx-whisper` (`mlx_whisper`) and `pyannote.audio` (`pyannote`) use this. Caller MUST `.detach()` in `finally`. |
| `attach_file_tail`  | Polls a growing log file from current EOF, emits LogLine per appended line. Used by the **detached** `vllm-mlx` server (owned by `ServerManager`; logs land in a file rather than a pipe so the server survives across CLI invocations). Handles truncation / rotation by resetting offset on shrink. |

All four surfaces share a hard cap of 5000 lines per (source, op_run);
past that a single `LogLine(level="warn", line="log truncated past 5000
lines")` is emitted and the source goes quiet. Keeps a runaway
backend from flooding the SSE queue.

Both `Progress` and `LogLine` events emitted from inside an op now
carry `job_id` (via `OperationContext.job_id`), so the per-job SSE
endpoint `GET /jobs/{id}/events` includes them in replay + live
delivery. The fix: every emitter that previously passed
`op_run_id=run_id` now also passes `job_id=ctx.job_id`. This was a
pre-Phase-A latent bug that surfaced when the Logs tab assertion
in `verify_observability.sh` was added.

**Web UI consumption** (`web/src/routes/jobs/[id]/+page.svelte`):

* New **Logs tab** between Events and Op runs. Filters SSE frames to
  `type == "log_line"`, keeps them in a dedicated 2000-entry buffer
  (separate from the 500-entry general events buffer so heavy stdout
  traffic doesn't crowd out `Progress` / `op_completed`). Per-source
  `<select>` populated from the observed event tail. Auto-scrolls to
  bottom by default; pauses when the operator scrolls up and shows a
  `tail ↓` resume button.
* **Status-header gauges** — `RAM x.x GB` + `ETA Nm SSs` pills next
  to the job status pill, sourced from the most recent
  `Progress(phase="heartbeat")` frame. Hidden once the job hits a
  terminal state so a stale snapshot doesn't misrepresent a finished
  run. `data-test="job-ram-gauge"` / `job-eta-gauge` are stable
  Playwright selectors.

Operator-invoked regression gate at
`bash scripts/verify_observability.sh` (3 specs): tab presence + click,
source filter wired, ffmpeg LogLines surface within 10 s of submitting
`video.extract_audio` on the bundled fixture.

## 13. `video.comprehend` (Phase 6.7)

The most complex composite shipped to date. End-to-end Video →
Analysis via five existing ops + an inline per-frame fan-out + one
SOTA-LLM synthesis call:

```
video.comprehend(Video)
├── video.extract_audio       → Audio
├── audio.transcribe_diarized → Transcript (segments + speaker_id)
├── (release_audio_models)    drops whisper singleton + pyannote slots
├── video.sample_frames @ fps → FrameSet
├── frames.analyze × N        per-frame VLM (asyncio.Semaphore-throttled)
├── (release_server)          tears down vllm-mlx if it was the backend
├── timeline merge inline     → MarkdownArtifact (sorted by t_sec)
└── intelligence.extract      (output_kind=structured, default schema)
    OR intelligence.summarize (output_kind=prose)
```

Hard guards at `run()` entry:

* `fps × effective_duration > max_frames` → `ValueError` with a
  suggested fps. Default `max_frames=240` (≈4 min @ 1 fps).
* `vlm_model` is `mlx-community/*` on a non-arm64 host → `RuntimeError`
  pointing at the deferred Linux backend.
* Empty timeline (no usable frame descriptions AND no transcript
  segments) → `RuntimeError`. Defensive; the synth model would
  hallucinate.

Per the B-008 routing rule (router model/backend consistency), the
per-frame fan-out and the final synth call pass `model=` but never a
hard `backend=` override — defer entirely to each delegate's model-
prefix router.

`records_cost=False`: every sub-op already bills its own spend, so
the composite stays out of the cost ledger to avoid double-counting.
`delegates_to` declares all six delegates honestly so `med doctor
--op video.comprehend [--json]` returns a per-delegate breakdown
(handled by the existing Phase 6.6 cycle-guarded walker).

Default profile at `profiles/examples/video-comprehend.yaml` — a
single-node DAG operators can paste into the Web UI Profiles
workspace and run. The default `vlm_model` assumes Apple Silicon; on
Linux swap to a `gemini-2.5-*` model.

---

*Companion docs:* `adding_an_operation.md` (how to add an op),
`adding_a_backend.md` (how to add a backend implementation, Phase 5),
`writing_a_profile.md` (YAML pipeline vs MD prompt),
`profile_analysis_full.md` (bundled starter pipeline, Phase 5),
`deployment.md` (env vars + volumes + probes + scaling, Phase 4),
`cli_reference.md` (every `med <verb>`, Phase 5),
`api_reference.md` (REST + MCP + Python API surface, Phase 5),
`quickstart.html` (executive overview + chronology).
