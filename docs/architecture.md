# media_engine — Architecture

> Comprehensive reference for the engine as built (Phases 0–2 complete,
> commits 1–22 + audit hardening). The commit-by-commit roadmap and
> capability charter live in the implementation plan
> (`~/.claude/plans/goofy-gathering-beaver.md`); this document describes
> the system that exists today, every module, and *why* each design
> choice was made — including the ones that diverge from the plan.

---

## 1. What this is

`media_engine` factors the media-processing capabilities of two existing
apps (`davos_video_grepper` — WEF video intelligence; `framepulse` —
single-video studio) into one substrate so future apps are written as
**profiles (data)**, not new programs (code).

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
Document, WebPage.

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

### 4.4 Op catalog (Phases 0–4 complete, 31 ops)

| Group | Ops | Backend layer |
|---|---|---|
| acquire | upload, url, livestream | upload: — (local-fs) · url: yt-dlp/playwright-hls · livestream: ffmpeg-recorder |
| metadata | scrape_page | — (embedded playwright, lazy) |
| transcript | parse, merge | — (pure-Python; one parser for srt/speakered_txt/vtt) |
| document | parse | pymupdf (unstructured deferred) |
| web | fetch | httpx (static); playwright (render_js=True) |
| search | semantic, fulltext, hybrid | semantic: sqlite / pgvector · fulltext: sqlite-fts5 / postgres-tsvector · hybrid: composite (RRF) |
| video | extract_audio, trim, sample_frames, multimodal | sample_frames: ffmpeg-uniform/pyscenedetect · multimodal: gemini/vllm-mlx |
| audio | transcribe, detect_language, diarize, transcribe_diarized | transcribe/detect: mlx-whisper · diarize: pyannote · t_d: composite |
| frames | subsample, analyze, compare | analyze: gemini/vllm-mlx · compare: gemini |
| image | describe, ocr, classify | describe: gemini · ocr: rapidocr/gemini-vision · classify: open-clip/gemini |
| chunk | semantic | default (nltk) |
| embed | text | sentence-transformers |
| intelligence | extract, summarize, classify, analyze | extract: mlx-lm/claude/gemini · others: composite |

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
-> disk-space gate -> workdir -> ctx (backend, run_op, emit, pools)
-> emit OpStarted
-> with_retry(op.run)         (policy: backend.retry_policy or by-name)
   |_ failure -> emit OpFailed(envelope) -> raise
-> record_run (cache row, idempotent on the key tuple)
-> record_cost (cost_log) — skipped when op.records_cost is False
-> emit OpCompleted
-> stamp produced_by, upsert artifacts, return
```

Events fan out to subscribers (daemon stream) and to a synchronous
persistence sink that writes the `events` table; the Engine prunes
events older than 7 days on open (best-effort, swallowed on error).

### 6.2 OperationContext

What an op receives: `workdir`, `config`, `storage`, `namespace`,
`emit`, `server_manager`, `model_pool`, `run_op` (composite recursion
handle), `backend` (engine-resolved name — the dispatch source of
truth), and `cache` (read-only handle, set by `Engine.run`, used by
index-building ops like `search.*` to enumerate persisted artifacts
across runs — `None` outside Engine.run). Resource semaphores are
acquired by the DAG executor *around* the op, so ops stay declarative
and never touch a lock.

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
- **REST (`api/`)** — FastAPI app (Phase 4 commit 29). Endpoints:
  `POST /run`, `POST /pipelines` (both return 202 + `job_id`),
  `GET/DELETE /jobs[/id]`, `GET /jobs/{id}/events` (SSE through
  `sse-starlette`), `GET /artifacts[/id][/file][/lineage]`,
  `GET/POST /profiles`, `GET /operations[/name]`,
  `GET /backends[/name]`, `POST/GET/DELETE /tokens`, plus
  un-authenticated `/health` + `/ready` probes. Bearer-token auth
  (32-byte secrets hashed at rest in `api_tokens`); the token's
  namespace scopes every read/write. `med api start` boots uvicorn
  against `build_app()`; `med api token create` mints the first
  bootstrap token directly against the cache (no chicken-and-egg).
- **MCP (`mcp/`)** — `exporter.py` turns every registered op into an MCP
  tool (`.`->`__`, params schema + `input_artifact_ids` + `backend`
  enum; variadic ops get an unbounded `minItems:1` array).
  `server.py` (Phase 4 commit 30) is the stdio server: `tools/list`
  filtered by `MCPSecurityConfig` (default = read-only `search.*`),
  `tools/call` dispatches to `Engine.run`, `resources/list` +
  `resources/read` surface artifacts as `media://<kind>/<id>`. `med
  mcp serve [--allow OP] [--deny OP]` is what
  `claude mcp add media-engine "med mcp serve"` invokes.

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

The same recipe works for the heavier davos / framepulse profiles
landing in Phase 5 — the math is identical, just with more nodes.

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
├── ops/                   _base · _registry · <group>/<verb>.py (31 ops)
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

**Phases 0–3 complete** (commits 1–28 + three audit-fix commits);
**Phase 4 complete** (commits 29–34). Phase 4 added the FastAPI REST
surface + `Job` concept + bearer-token auth (commit 29); the full MCP
stdio server with read-only-by-default allow-list (commit 30); the
Postgres / pgvector / postgres-tsvector backends + alembic migrations
+ `med db migrate|dump-sqlite-to-postgres` (commit 31); LRU eviction
+ workdir GC + `med storage stats|gc|migrate` (commit 32); the IaaC
package (Dockerfile, docker-compose, Helm chart, Terraform module),
`/health` + `/ready` probes + `med health|ready`, and
`docs/deployment.md` (commit 33); and the `resources.yaml` loader
(commit 34). **31 ops.** Suite: 699 passed / 29 skipped
(dependency/API-key/network gated); `ruff` and strict `pyright` clean.

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

Audit-driven correctness fixes are called out inline as *Design note
(audit fix)*. Reconciliation commits: `fix(phase-1): close audit
findings`, `fix(phase-2): close pre-Phase-3 audit findings`,
`fix(phase-3): close pre-Phase-4 audit findings`, `fix(phase-4): close
pre-Phase-5 audit findings`.

Phase 4 (REST + full MCP + Postgres + IaaC) is complete; Phase 5
(domain profiles + speakers + reports + final polish) is the next
work.

---

*Companion docs:* `adding_an_operation.md` (how to add an op + backend),
`writing_a_profile.md` (YAML pipeline vs MD prompt), `deployment.md`
(env vars + volumes + probes + scaling, Phase 4). A REST/MCP API
reference is scheduled for Phase 5 commit 38.
