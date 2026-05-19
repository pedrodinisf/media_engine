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
Transports   cli/  ·  daemon/  ·  mcp/        (REST = Phase 4)
                 \      |        /
                  v     v       v
Engine        runtime/engine.py  -- runtime/dag.py (async DAG executor)
                  |
Ops           ops/<group>/<verb>.py        capability-named verbs
                  |  select_backend / ctx.backend
Backends      backends/<group>_<verb>/<provider>.py   swappable impls
                  |  read/write
Artifacts     artifacts/  typed, frozen, content-addressed (sha256)
                  |  persisted / indexed
Runtime infra storage.py · cache.py · events.py · cost_tracker.py · ...
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

### 4.4 Op catalog (Phases 0–2 + Phase 3 in progress, 24 ops)

| Group | Ops | Backend layer |
|---|---|---|
| acquire | upload, url, livestream | upload: — (local-fs) · url: yt-dlp/playwright-hls · livestream: ffmpeg-recorder |
| metadata | scrape_page | — (embedded playwright, lazy) |
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
| `cache.py` | SQLAlchemy 2.0 (SQLite + WAL pragmas). Tables: `cached_artifacts`, `cached_operation_runs` (unique on the cache-key tuple), `cost_log` (append-only spend ledger), `events` (durable event tail). `to_orm`/`to_pydantic` are the only Pydantic<->ORM crossings. |
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
truth). Resource semaphores are acquired by the DAG executor *around*
the op, so ops stay declarative and never touch a lock.

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
  segmentation); `med cost ls|summary`; `med events tail|history`; `med daemon|mcp`.
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
- **MCP (`mcp/`)** — `exporter.py` turns every registered op into an MCP
  tool (`.`->`__`, params schema + `input_artifact_ids` + `backend`
  enum; variadic ops get an unbounded `minItems:1` array). `med mcp
  tools-json` today; full stdio server in Phase 4.

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

---

## 10. Module map (one line each)

```
media_engine/
├── __init__.py            public re-exports (Engine, Pipeline, Artifact, …)
├── bootstrap.py           register_all() — the op + backend catalog
├── config.py              pydantic-settings, MEDIA_ENGINE_* env, config.toml
├── logging_setup.py       text default, JSON via MEDIA_ENGINE_LOG_FORMAT
├── artifacts/             base (Kind/Artifact/hashing) · media · text · analysis
├── ops/                   _base · _registry · <group>/<verb>.py (24 ops)
├── backends/              _base · _pricing · _gemini_vision · <group>_<verb>/<provider>.py
├── runtime/               engine · cache · storage · dag · retry · events
│                          cost_tracker · lineage · model_pool · server_manager
│                          hardware · disk_guard · ffprobe · jsonschema
├── profiles/              schema · loader · pipeline
├── cli/                   __init__(entry) · _handle · run/cost/events/profile/
│                          daemon/mcp/batch
├── daemon/                protocol · server · client · entry
└── mcp/                   exporter
```

---

## 11. Status & deviations from the plan

**Phases 0–2 complete** (commits 1–22 + two audit-fix commits); **Phase
3 in progress** (commits 23–24: `acquire.url`, `metadata.scrape_page`,
`acquire.livestream` + `med acquire-live`). Suite: 536 passed / 22
skipped (dependency/API-key/network gated); `ruff` and strict `pyright`
clean.

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
  the `extract_invoke` non-persisting hook, `runtime/jsonschema.py`.

Audit-driven correctness fixes (commit `fix(phase-2): close pre-Phase-3
audit findings`) are called out inline above as *Design note (audit
fix)*.

Phase 3 (acquisition + transcript ingest + non-video media + search,
commits 23–28) starts from a green, reconciled base.

---

*Companion docs:* `adding_an_operation.md` (how to add an op + backend),
`writing_a_profile.md` (YAML pipeline vs MD prompt). API reference and
deployment docs land in Phase 4.
