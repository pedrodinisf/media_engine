# Changelog

All notable changes to `media_engine` are tracked here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semver applies
once we ship v1.0 (after Phase 6 — the REST surface needs to freeze
first). Until then expect 0.x to bump frequently and best-effort
backwards compatibility.

## [Unreleased]

_Nothing yet._

## [0.8.0] — 2026-07-08

Phase 7 (acoustic speaker identity), plus the repo-hygiene / public-release
pass and five small `video.comprehend` refinements that landed after the
v0.7.0 tag.

### Added

- **Phase 7 — acoustic speaker identity** (v0.8.0). Three new ops:
  `speakers.embed_voice` (per-diarization-turn voice fingerprints via
  pyannote embedding), `speakers.cluster` (HDBSCAN → stable
  `Speaker_<sha8>` ids, reconciled against saved profiles), and
  `speakers.match` (cosine lookup vs the fingerprint DB; sqlite +
  pgvector backends). New artifact kinds `SpeakerEmbedding` /
  `SpeakerProfile`, a namespace-scoped `fingerprints.db` store,
  `med speakers` CLI group, and `profiles/examples/speaker-id.yaml`.
  Privacy-by-default: `speaker_storage_enabled` / `speaker_export_enabled`
  both off, MCP hidden, `med speakers purge` for per-namespace delete.
  New `cluster` extra (hdbscan + scikit-learn + numpy). See
  `docs/phase-7.md`.
- **CI** (`.github/workflows/ci.yml`) — ubuntu-latest jobs for
  `ruff check` / `pyright media_engine` / `pytest` (Python) and
  `svelte-check` / `vitest` (frontend web/).
- **LICENSE** — MIT.
- `video.comprehend` gains a `style="meeting"` mode + a bundled
  teams-meeting profile.

### Fixed

- **Phase 7 privacy hardening** (review pass): the `speaker_export_enabled`
  REST gate was enforced only on `POST /run`, so a pipeline DAG (`POST
  /pipelines`) could smuggle a gated `speakers.*` op past it — now enforced on
  both surfaces. `med speakers purge` / `Cache.purge_namespace` now also
  deletes the on-disk artifact blob files (voice-vector sidecars), not just
  the index rows. Plus robustness fixes: numpy-array flattening in the pyannote
  embedding backend, empty-query handling in `speakers.match`, `min_samples`
  clamping in the HDBSCAN backend, and a side-effect-free fingerprint-store
  path helper.
- **`web/pnpm-workspace.yaml`**: `allowBuilds.esbuild` was committed
  as the literal placeholder text `"set this to true or false"`
  instead of `true`, so pnpm's build-approval gate
  (`ERR_PNPM_IGNORED_BUILDS`) never cleared and `bash
  scripts/build_web.sh` — the exact command CLAUDE.md tells new
  contributors to run — failed on a clean checkout. `scripts/build_web.sh`
  also now runs `pnpm` via `corepack` when available, so it respects
  the `packageManager` pin in `web/package.json` instead of whatever
  `pnpm` major happens to be on the contributor's PATH.
- pyannote.audio 4.x `DiarizeOutput` wrapper unwrapped correctly.
- Ephemeral single-frame `FrameSet`s (produced by `video.comprehend`'s
  per-frame fan-out) are now registered in the cache and hidden from
  the catalog browser instead of cluttering it; `scripts/catalog_reset.py`
  ships to clear a namespace during development.
- `video.comprehend`'s hardware-gate error message is now actionable
  (names the offending `vlm_model` + how to fix it), paired with a
  smaller default VLM.
- `tests/test_op_video_comprehend.py::test_frame_budget_pre_flight_raises`
  didn't pin `vlm_model`, so on any non-Apple-Silicon machine it hit
  the (correct) hardware gate before ever reaching the frame-budget
  check it was meant to exercise — every sibling test in the file
  already pins `vlm_model="gemini-2.5-flash"` for this reason. Fixed
  to match.
- `tests/test_api_settings.py::test_secrets_impact_hf_unblocks_diarize`
  asserted `HF_TOKEN` alone unblocks `audio.diarize`, but the pyannote
  backend also hardware-gates on `apple_silicon`
  (`media_engine/backends/diarize/pyannote.py`) — the assertion only
  holds on that platform. Marked `@pytest.mark.needs_pyannote` to
  match how the suite's other hardware/credential-gated tests are
  excluded from a default run.
- Removed personal-environment fingerprints ahead of going public:
  the `/Volumes/UNIVERSE_V/…` default `permanent_store` path
  (now `~/.local/share/media_engine`), a stray name in a test comment,
  and a personal GitHub username hardcoded into the `web.fetch`
  User-Agent string.
- Removed a docs screenshot (`docs/web_ui/profile-workspace.png`)
  that leaked a local filesystem path.
- `video.extract_audio` / `audio.detect_language` / `sample_frames`
  gain `start_s`/`end_s` windowing; `video.comprehend` forwards it.
  Web UI range slider now lights up for Video inputs, not just Audio.

### Fixed — correctness sweep

A pass over all 35 ops surfaced a cluster of related defects, each
landed with a regression test verified to fail on the pre-fix tree:

- **`video.comprehend` ignored the time window on the audio track.**
  The composite forwarded `start_s`/`end_s` to `video.sample_frames`
  and `audio.transcribe_diarized` but called `video.extract_audio`
  with no params, so a 10 s window transcribed 10 s but extracted the
  full audio track — a cache miss + a latent duration mismatch for any
  downstream consumer. Now forwards symmetrically.
- **Five `Progress` event sites never reached the Web UI.**
  `acquire.url` (yt-dlp), `acquire.livestream` (ffmpeg-recorder +
  playwright-HLS), `video.multimodal` (gemini upload), and
  `metadata.scrape_page` constructed `Progress(...)` without
  `job_id=ctx.job_id`; the SSE per-job filter dropped them, so the
  Job-detail Events tab stayed empty during those ops. (Same class as
  the Phase-6.7 `job_id` sweep, which missed these five.)
- **Op + profile default models were too large for 16 GB machines.**
  `video.comprehend`'s `vlm_model` default dropped 7B → 2B (the
  profile default had already moved but the op default lagged); the
  `analysis-full` profile dropped its analysis model 14B → 7B and the
  `video-comprehend` example profile 7B → 2B, each documenting the
  larger-hardware opt-in inline.
- **Vague error messages** on `chunk.semantic` / `embed.text` /
  `audio.diarize` (no-default-backend) and `transcript.parse`
  (unknown-format) now name the fix — the `backend=` argument and the
  list of valid formats respectively.
- **Missing numeric bounds** on `intelligence.{classify,summarize,analyze}`
  (`temperature`, `max_tokens`) and `search.hybrid` (`top_k`, `rrf_k`)
  now carry `ge`/`le` constraints, so the Web UI auto-form renders
  guard rails and the backend rejects nonsense at validation time.
  `analyze`'s hand-rolled `window` validator was replaced by
  `Field(ge=1)` (same semantics; standard schema-emitted message).
- **`release_audio_models` failed strict pyright on Linux CI** (the
  very CI added in this release). `mlx.core` has no Linux wheels, so
  the existing `# type: ignore[import]` on the import line suppressed
  the import-resolution error but not the `reportUnknownMemberType`
  cascade on every subsequent `mx.clear_cache` / `mx.metal` attribute
  access — pyright had only ever been run against this file with mlx
  actually installed (macOS). Fixed by explicitly typing the bound
  name `Any`, matching the existing idiom in
  `backends/transcribe/mlx_whisper.py`.

### Suite

1050 passed / 6 skipped / 24 deselected (hardware, API-key, and
external-tool gated — see the `needs_*` markers in `pyproject.toml`),
verified on the actual GitHub Actions Linux runner, not just locally.
Ruff clean. Pyright strict clean on Linux. Frontend: 70 Vitest unit
tests, svelte-check 0/0 on 582 files.

## [0.7.0] — 2026-05-26

Phase 6.7 — two related shipments bundled into one release because
the second leans on the first for debugging UX.

### Added

- **Live observability**: every running op now emits a
  `Progress(phase="heartbeat", ...)` event every 2s carrying
  available RAM, an ETA derived from `op.cost_estimate(...)`, and
  the model-pool byte estimate (`runtime/heartbeat.py`, wired in
  `runtime/engine.py`).
- The previously-defined-but-never-emitted `LogLine` event now
  flows: `runtime/log_pump.py` exposes `attach_subprocess()`,
  `attach_logger()`, and `attach_file_tail()`; ffmpeg (in
  `extract_audio` + `sample_frames/ffmpeg_uniform`), mlx-whisper,
  pyannote, and the vllm-mlx server file-tail all forward
  stdout/stderr or library loggers.
- `OperationContext` gains `job_id` + `op_run_id` fields, carried on
  both `Progress` and `LogLine`, so per-job SSE replay surfaces them
  on `/ui/jobs/[id]`.
- Job-detail page: a **Logs tab** (per-source filter, auto-scroll,
  2000-line dedicated buffer) and live RAM/ETA gauges in the status
  header.
- `scripts/verify_observability.sh` — operator-invoked regression
  gate for the Logs tab + gauges + in-op SSE replay (3 specs).
- **`video.comprehend`** — new composite op. Fans out per-frame VLM
  calls at a user-chosen fps (vllm-mlx on Apple Silicon; cloud
  gemini on Linux), runs `audio.transcribe_diarized`, merges both
  timelines into a `MarkdownArtifact`, and feeds that to one SOTA
  LLM call (`intelligence.extract` for `output_kind=structured` /
  `intelligence.summarize` for `prose`). Hard-fails fast on
  `fps × duration > max_frames` (default 240) and on Linux + an mlx
  `vlm_model`. Default profile at
  `profiles/examples/video-comprehend.yaml`.
- `tests/test_op_video_comprehend.py` — 10 unit specs (fan-out,
  timeline merge, derived-id determinism, `output_kind` routing,
  hardware gate).

### Changed

- The RAM-release helper from `transcribe_diarized` was renamed
  `release_audio_models` and now also drops `pyannote:*` slots from
  `ctx.model_pool`; the vllm-mlx backend exports a sibling
  `release_server(ctx)`.

## [0.6.2] — 2026-05-25

Phase 6.6 — audit + close-out. Closed every bug left open in the
Phase 6.5 triage (`docs/phase-6-5-bugs.md`).

### Fixed

- **B-004 (p1)** — locale-safe float input. `FloatInput.svelte` keeps
  a period-decimal text buffer (seeded from `String(value)`, so it's
  locale-independent) so pt-PT users see `0.2`, not `0,2`.
- **B-006 (p2)** — intelligence model dropdown via
  `json_schema_extra` + a new `media_engine/ops/intelligence/_models.py`
  catalog, mirroring the audio-ops pattern.
- **B-007 (p1)** — composite ops with backend routers now forward
  `--backend` overrides to their delegates. Precedence: explicit
  composite param > `ctx.backend` > delegate's own model-prefix
  router.
- **B-008 (p1)** — router model/backend consistency is now a hard
  fail at submit time and in `POST /run/preview`, instead of silently
  dispatching the wrong backend to the wrong model.
- **B-009 (p2)** — `med doctor` walks `delegates_to` for embedded
  composites and surfaces a per-delegate breakdown
  (`OpDoctorReport.delegate_overalls`), with a cycle guard; Settings →
  Doctor renders it in the expand row.

### Added

- `Operation.delegates_to` static declaration on every composite op,
  reused by the doctor recursion above and by the Settings → Secrets
  impact computation.
- E2E regression specs in `scripts/verify_settings.sh` grew from 11
  to 14.

## [0.6.1] — 2026-05-23

Phase 6.5 — a focused post-release quality + UX pass. Started from
the user's manual smoke session against v0.6.0, which surfaced
three concrete bugs in the new Web UI flows + a deeper pattern:
**the op → backend → dependency contract was declared in code
(`BackendRequirements`) but never reachable to operators**. So
when the Web UI form rendered fine and the cost preview computed
fine but submit silently failed deep in `Engine.run`, there was
no diagnostic surface to fall back on. This release fixes the
known bugs *and* builds the introspection surfaces (`med doctor`
+ op matrix runner) that turn that class of bug into a single
green/red command.

### Added

- **`med doctor [--op N] [--json]`** — declarative dep map per op
  + backend. Walks every registered op, evaluates each backend's
  `BackendRequirements` (env vars, binaries on PATH, importable
  Python packages, hardware tag, RAM) against the live env, prints
  a green/red matrix, exits non-zero if any op has no working
  backend. `--op X` filters to one op or a prefix; `--json` for CI.
  Non-router ops roll up to the *default backend's* status (a
  working alternative doesn't help if the default route is broken);
  router ops keep "ok if any" semantics with the deep view flagging
  when the default is the broken one. ~300 LOC + 23 unit tests.
- **Op matrix runner (`scripts/op_matrix.py`)** — walks every
  registered op, attempts execution through `Engine.run` against
  vendored fixture artifacts (one per Kind), classifies as
  ✓ / ⊘ / ✗, writes `tests/e2e_op_matrix_report.md`. Runtime
  failures matching known dep-gap patterns are reclassified as ⊘
  so ✗ truly means engine bug. Pre-filters via doctor; steers
  through a working alternate backend when the default is broken
  but the router has alternatives. Operator-invoked
  (`uv run python scripts/op_matrix.py`); not part of the pytest
  gate. Current result: ✓ 14 · ⊘ 20 · ✗ 0.
- **`scripts/verify_b001.sh`** — boots a clean `med web start`,
  mints a token, drives `web/tests/e2e/flows/sse_events.spec.ts`
  through real Chromium asserting the Job-detail Events tab
  populates within 5s, tears down. `--headed` to watch live.
  Regression gate for B-001.
- **`docs/phase-6-5-bugs.md`** — triaged bug log (now 7 open: 1
  p1, 6 p2). Future sessions resume here.
- New schema column `events.job_id` (+ `idx_events_job` index) —
  the persistent event log can now be queried by REST/CLI
  submission id, decoupled from `op_run_id` (which is per-op).
  Alembic migration `0002_events_job_id`.
- `Engine.run` and `Engine.run_pipeline` accept an optional
  `job_id` kwarg. When provided, emitted `Event.job_id` carries
  it (used by the REST surface to correlate SSE streams with
  submitted jobs). `ctx.run_op` is wrapped in a closure that
  pre-fills the parent's `job_id` so composite sub-op events stay
  correlated automatically.

### Fixed

- **B-001 (p0) — Job-detail Events tab populates within seconds
  of submission**, was "Waiting for events…" indefinitely. Three
  independent root causes, all closed:
  1. `Engine.run` minted its own internal id and stamped events
     with it; the REST `job_id` never reached the SSE filter.
     Engine.run now accepts the caller-supplied `job_id`;
     `submit_run_op` + `submit_pipeline` pass it through.
  2. The Web UI SSE wrapper listened for `OpStarted/...`
     (PascalCase) but the server emits `op_started/...`
     (snake_case from `Event.type` literals); every named-event
     frame was dropped on the floor. Aligned both sides on
     snake_case.
  3. A client subscribing AFTER an event has fired (the common
     race for fast ops) missed it; `EventBus.subscribe()` only
     delivers events emitted after registration. Added
     replay-on-subscribe to `api/sse.py`: the pumper queries the
     persistent `events` table by `job_id`, yields persisted
     frames first, then switches to live mode with `event_id`
     dedup against the replayed set.
  - Regression coverage: 3 new `tests/test_api.py` tests +
    Playwright spec against a live `med web start`. Verifier
    run on this machine: ✓ 292ms (5s budget).
- **B-003 (p1) — `med api token create --namespace` reads
  `MEDIA_ENGINE_NAMESPACE`**. Before, it defaulted to literal
  `"default"`, so tokens minted on a non-default engine 403'd
  every authed endpoint (`require_token` enforces ns parity).
  `cmd_token_create` now defaults from `EngineConfig().namespace`
  when `--namespace` is unset. Regression covered.
- **B-010 (p2) — under-declared backend deps**.
  `transcribe/mlx_whisper`, `document/pymupdf`, and
  `embed_text/sentence_transformers` declared `BackendRequirements`
  without their `services=[…]` Python-package entries; doctor
  reported them green even when missing and users hit opaque
  `RuntimeError: X is not installed` at runtime. Manifests
  corrected.
- `EventLogEntry.id` now uses `Event.event_id` as the row PK
  (was a fresh `uuid4()`), so replay/live dedup against the bus
  stream works without parsing JSON payloads.

### Changed

- `cache.event_log()` accepts `job_id=` filter and `order=` param
  (`"desc"` default — preserves existing `med events history`
  behaviour; `"asc"` for SSE replay's causal-order requirement).
- `cache.record_event()` accepts optional `event_id=` (used as
  the row PK when provided) and `job_id=` (persisted alongside
  `op_run_id`).
- README + `CLAUDE.md` + `docs/architecture.md` §11 + `docs/api_reference.md`
  + `docs/cli_reference.md` + `docs/adding_a_backend.md` updated to
  reference the new doctor surface, the SSE replay semantics, the
  snake_case event-name protocol, and the lesson learned about
  declaring every optional dep in `BackendRequirements`.

### Upgrade notes

- **Sqlite users on a fresh `Cache()`-created DB**: the new
  `events.job_id` column is created automatically via
  `Base.metadata.create_all`. No action required.
- **Sqlite users upgrading an existing 0.6.0 DB, OR any Postgres
  deployment**: run `med db migrate` to apply
  `0002_events_job_id`. Until then, SSE replay silently degrades
  to live-only mode (the client-side snake_case fix is
  schema-independent, so live mode still works once the timing
  race is over).
- No artifact-cache schema change — `cache_artifacts` etc.
  unchanged.

### Suite

922 passed / 29 skipped (was 894 / 29). Ruff + strict pyright
clean. Frontend: 54 Vitest unit tests, svelte-check 0/0 on 572
files. New: `tests/test_doctor.py` (23 tests),
`tests/e2e_op_matrix_report.md` (regenerable matrix report),
`web/tests/e2e/flows/sse_events.spec.ts` (Playwright B-001
regression). Commits: 4 (`feat(cli): med doctor`, `feat(qa):
op matrix + doctor enhance + bug log`, `fix(api/web): SSE
events deliver to job-detail (B-001 p0) + B-003 p1`,
`test(e2e): browser-driven B-001 regression spec`).

## [0.6.0] — 2026-05-22

Phase 6 (local-first Web UI, plan §12.5) closes. Twelve numbered
commits (39–50) plus three audit-fix passes (post-46, post-48,
post-49) and two docs syncs landed in the release window. The engine
now ships a SvelteKit SPA bundled at `/ui`, served by the same
FastAPI process as REST: ingest, run, jobs, catalog (+ detail +
lineage), search, cost, profile workspace, and a six-tab settings
panel are all live with full CLI parity. Commit 50 adds the `+ui`
multi-stage Dockerfile (Node-free runtime image), six bundled
screenshots (regenerable via `scripts/gen_ui_screenshots.sh`), and
the `docs/quickstart.html` Web UI + Profiles expansion.

### Added

- **Web UI (`web/` source, built into `media_engine/web/dist/`)** —
  SvelteKit 2 / Svelte 5 SPA bundled at `/ui`, served by the same
  FastAPI process as REST. Panels live today: Ingest (upload / URL
  probe / livestream / batch), Run (op picker + schema-driven form
  + backend health badges + live 250 ms cost preview), Jobs (live
  table + per-job detail with SSE events / op runs / outputs /
  failure envelope + cancel), Catalog (paginated list + kind chip
  filter + per-kind preview affordances), Catalog detail (Preview /
  Metadata / Lineage tabs with Svelte Flow + dagre graph viewer),
  Search (debounced live query, fulltext / semantic / hybrid, top-k
  slider, kind filter), Cost (per-{op,backend,namespace} rollup
  bars + monthly burn projection + paginated drill-down ledger).
  Tailwind v4 Clean-NASA tokens lifted from `docs/quickstart.html`;
  TypeScript strict + `exactOptionalPropertyTypes`. See
  [`docs/web_ui.md`](docs/web_ui.md) for the panel-by-panel tour.
- **`med web start [--host] [--port] [--open/--no-open]`** — CLI
  launcher. Validates `media_engine/web/dist/` is present; same
  uvicorn boot as `med api start` plus the `/ui` static mount and
  an optional browser auto-open.
- **REST surface widened** (additive — no Phase-4 endpoints
  changed):
  - `POST /run/preview` — cost-only `Engine.estimate_op_cost`
    without submitting a job. Web UI run panel debounces it
    (250 ms).
  - `POST /acquire/upload` — multipart upload with ffprobe preview
    (`commit=false`) + commit (`commit=true → acquire.upload`
    job). Honors `MEDIA_ENGINE_MAX_UPLOAD_MB` (default 2048).
  - `POST /acquire/url/probe` — yt-dlp `--dump-single-json`
    metadata-only resolve.
  - `POST /search` — sync wrapper around `search.{fulltext,semantic,
    hybrid}` with a 30 s timeout and `top_k` bounded at 200.
    Unwraps the Analysis output's `results: [...]` into a bare
    ranked list. Semantic + hybrid embed the query string via
    `runtime/search_query.py` (shared with `med search`).
  - `GET /cost/summary?group_by=op|backend|namespace` — per-key
    spend rollup over the `cost_log` table.
  - `GET /cost/log` — paginated newest-first ledger with
    since/until/op filters + offset+limit pagination.
  - `GET /events/stream` — global SSE tail across every job.
  - `GET /events/history` — durable event tail.
  - `?token=...` shim on SSE routes (`/jobs/{id}/events`,
    `/events/stream`) for `EventSource` clients.
- **Static mount + middleware** —
  `media_engine/api/middleware.py::UISecurityHeadersMiddleware`
  adds CSP (with `wasm-unsafe-eval` for `pdf.js` and
  `style-src 'unsafe-inline'` for Svelte scoped styles) +
  `X-Content-Type-Options: nosniff` + `Referrer-Policy:
  same-origin` to `/ui/*` responses only. Defaults to same-origin
  CORS; opt in via `MEDIA_ENGINE_CORS_ORIGINS`.
- **Wheel packaging** — `[tool.hatch.build.targets.wheel.force-include]`
  ships the built `media_engine/web/dist/` tree with every wheel.
  `pip install media_engine[api]` gets the UI for free; no Node
  toolchain needed on production hosts.
- **`scripts/build_web.sh`** — wraps `pnpm -C web install
  --frozen-lockfile && pnpm -C web build` for CI / wheel-build
  use.
- **Frontend test infra** — Vitest unit suite + Playwright e2e
  scaffold under `web/tests/`. 47 unit tests today (schema-form
  renderer, lineage layout, artifact REST helpers, token store,
  cost / search format helpers, datetime-local local↔UTC bridge,
  YAML↔graph round-trip, profile-name validator + fork payload).
- **Profile workspace** (commit 47) — split-view route at
  `/ui/profiles/[name]` combining a visual DAG composer (Svelte
  Flow + dagre) with a CodeMirror 6 YAML editor (YAML mode +
  history + op-name autocomplete from `GET /operations`) + live
  compile via `POST /profiles/validate` (650 ms total debounce —
  150 ms parse + 500 ms validate). YAML is the canonical source
  of truth; edits round-trip through the `yaml` JS lib's
  `Document` model so comments + key order on the rest of the
  file survive byte-identical. Sources picker modal lets the user
  bind declared inputs to in-namespace artifacts before
  submitting a run via `POST /pipelines` (with `pipeline_yaml`
  inline so unsaved drafts execute).
- **Profile examples library + fork-this** (commit 48) — the
  `/ui/profiles` index now shows the 8 bundled profiles + every
  user profile as a card grid, each with a lazy body excerpt and
  (for bundled profiles) a one-click **fork** modal that
  validates a kebab-case name client-side, then POSTs the
  renamed copy to `{config_dir}/profiles/` and opens it in the
  workspace.
- **REST: `POST /profiles/validate`** (commit 47) — compile-checks
  a YAML body without persisting. Always 200; `ok` boolean +
  typed error envelope (`error_class`, `message`, 1-based `line`).
  Backs the workspace's live-compile indicator.
- **REST: `DELETE /profiles/{name}`** (commit 47) — removes a
  user-overrideable profile from `{config_dir}/profiles/`. Bundled
  profiles in `<repo>/profiles/` are never touched (the resolver
  scopes itself to the user dir).
- **REST: `ProfileSummary.source`** (post-commit-48 audit) —
  server-supplied `"bundled" | "user"` discriminator on every
  `GET /profiles` row so the Web UI doesn't need a path heuristic
  to tell read-only bundled profiles from editable user ones.
  `POST /profiles` always returns `source: "user"`.
- **New helper: `profiles.loader.load_profile_from_string`**
  (post-commit-48 audit) — parses a YAML string straight to a
  typed `Profile` without writing to disk. `POST /profiles/validate`
  uses it on the hot path.
- **CodeMirror 6 + `yaml` JS lib + `@xyflow/svelte` deps** — added
  for the profile workspace.

### Changed

- `MEDIA_ENGINE_MAX_UPLOAD_MB` env var added (default 2048;
  applies to `POST /acquire/upload`).
- `MEDIA_ENGINE_CORS_ORIGINS` env var added (empty default =
  same-origin only).
- `MEDIA_ENGINE_NO_BROWSER` env var added (forces
  `med web start` to skip the browser auto-launch).
- `_cli.search.query` op_name in `runtime/search_query.py` is
  preserved verbatim across the CLI-to-shared-runtime extraction
  so existing cached query-embedding rows still hit. The leading
  `_cli.` reads odd outside the CLI now but is an opaque cache-key
  seed; renaming would silently invalidate every prior search
  cache row. Documented in `architecture.md` §11.

### Fixed (post-release Phase 6 audits)

Three audit passes ran inside the v0.6.0 release window. Per-pass
granularity is preserved in git history (`fix(phase-6): post-commit-46
audit`, `fix(phase-6): post-commit-48 audit`, `fix(phase-6):
post-commit-49 audit`); the consolidated findings:

- **`/cost/log` `until` filter applied AFTER engine `limit`**
  shrank the candidate set, so far-back matching rows were
  invisible whenever `until` was in the past. Branched on
  `until is None` — keep the bounded fetch in the common path,
  drop to unbounded fetch + Python filter when `until` is set.
  Regression test seeds 100 newer-than-cutoff + 5 older rows
  and asserts the older 5 show through `limit=50`.
- **`/ui/cost` datetime-local inputs were broken end-to-end.**
  The HTML datetime-local spec requires `YYYY-MM-DDTHH:mm` (no
  Z, no sub-second precision); the initial state passed
  `toISOString()` output so inputs rendered blank. Worse,
  user-typed values were sent raw — local-time semantics on
  the client, naive-UTC on the server, off by the user's tz
  offset. Added `isoToLocalInputValue` + `localInputValueToIso`
  helpers in `$lib/api/cost.ts`, route user edits through
  commit helpers that update both the local-string and the
  canonical UTC-ISO state in lockstep, labels marked "(local)"
  to set expectations.
- **Burn-rate projection used live `since`/`until` state** instead
  of the summary's echoed window, so editing the inputs without
  Refresh drifted the projection from the displayed rollup.
  Anchored to `summary.since` / `summary.until` fallbacks.
- **Initial `/ui/cost` load race** — the `$effect` gate
  (`summary !== null`) raced with `onMount`. Restructured to
  let the `$effect` drive both initial + group-by re-fetch;
  drop the redundant `onMount` summary call.
- **`cost_routes` docstring polish** — no longer claims "scopes
  to `token.namespace`" (`require_token` forces the token's
  namespace to equal the engine's; the actual scope is the
  engine's). `monthlyBurnProjection` comment now matches
  implementation ($0 spend → $0, not null).
- **`POST /profiles/validate` did synchronous tmp-file disk I/O
  on every keystroke** — the pre-audit path created a workdir,
  wrote the YAML, called the path-based loader, then unlinked +
  rmdir'd. With the workspace firing validate every 500 ms of
  idle, the per-request syscall cost (5 calls: mkdir + write +
  read + unlink + rmdir) became a measurable hotspot. Added
  `profiles.loader.load_profile_from_string` and rewired the
  route to parse YAML straight from memory. Regression test
  asserts the workdir tree is unchanged after a validate call.
- **Profile workspace did a full `parseProfileText` + dagre
  re-layout on every keystroke** — the YAML editor pushed an
  update to `yamlText` on every change, all `$derived`
  consumers (parsed graph, composer layout, per-node editor)
  re-ran synchronously, and dagre layout for a 20-node pipeline
  is non-trivial (~5–50 ms). Added a 150 ms debounce: heavy
  consumers now read `yamlForLayout` (a debounced view) while
  the editor itself still binds `yamlText` 1:1. Single repaint
  per typing pause instead of per keystroke; validate's outer
  debounce continues from there (650 ms total before a network
  call).
- **YAML-driven rename was a silent footgun** — editing the
  top-level `name:` key in the YAML pane and hitting Save
  created a NEW profile at `{newName}.yaml` while leaving the
  original file in place. Added an explicit guard: Save refuses
  when `parsed.name !== route name`, with a hint pointing at
  the fork-then-delete workflow.
- **`ProfileSummary.source` field replaces the FE
  `/config/`-substring heuristic** for bundled vs user. The
  pre-audit heuristic was wrong for non-default config dirs;
  the server now stamps `source: "bundled" | "user"` on every
  `GET /profiles` row. Three regression tests cover user, bundled,
  and POST-returns-user.
- **Vestigial `untrack(() => {})` block in `appendNodeFromPalette`**
  removed (read no state, did nothing).
- **`loadProfileBody` errors now surface in their own UI slot**
  (previously stuffed into `saveError`).
- **`/ui/profiles/[name]` dropped its dynamic `import('yaml')`**
  — the editor already pulls yaml in, so a top-level
  `import { stringify as yamlStringify } from 'yaml'` removes
  a microtask delay per workspace mount.
- **Stale "lands in commit 48" comment** in the per-node card
  rewritten — commit 48 shipped the examples library, not the
  per-node SchemaForm; that's a smaller follow-up tracked in
  `web_ui.md` §10.
- **Reuse `state.engine.cache` in `/storage/stats` + `/storage/gc`**
  instead of constructing a fresh `Cache(...)` per request. The
  pre-fix path spun up a new SQLAlchemy engine + connection pool on
  every Settings tab activation; reusing the long-lived cache on the
  engine handle removes that overhead.
- **Hoisted lazy imports**: `from sqlalchemy import select` (in
  `/storage/stats`) and `from media_engine.runtime.plugins import
  load_catalog` (in `mcp/server.py:_filtered_op_names`) moved to
  module-level. Both were on hot paths — the MCP filter fires on
  every `tools/list` call.
- **Inlined `_catalog_response(state)` helper** so PUT
  `/plugins/catalog` returns the recomputed view without re-calling
  the GET handler through a `# type: ignore`-masked direct call.
- **Lifted `declared_resources` onto `OperationSummary`** so the
  Web UI's Settings → Config tab renders per-op resource
  allocations in **one** HTTP request, not N+1 detail fetches. The
  field stays on `OperationDetail` (it's inherited).
- **Regression test: `_EXTRAS_CATALOG` parity with pyproject.toml**.
  The hard-coded extras list in `api/plugins.py` would silently
  drift if pyproject gained a new extra; the test now asserts
  bidirectional parity and fails CI if anyone adds an extra without
  updating the route.

### Fixed (commit 50, discovered while regenerating screenshots)

- **`/ui` CSP blocked SvelteKit's inline boot script.** The
  `UISecurityHeadersMiddleware` shipped `script-src 'self'
  'wasm-unsafe-eval'`; SvelteKit's adapter-static index.html
  contains an inline `<script>` block that wires
  `__sveltekit_<hash> = { base, assets }` + the dynamic-import
  Promise. Without `'unsafe-inline'` on script-src the SPA never
  hydrated — every browser hit got a blank page. Added
  `'unsafe-inline'` to script-src with a documented tradeoff
  (loopback-first, same-origin only, no third-party scripts;
  v1.x hardening path is httpOnly cookies). Regression test in
  `tests/test_api_ui_mount.py` asserts the directive is present.
- **`/ui/<deep-path>` returned a 404 JSON.** `StaticFiles(html=True)`
  serves index.html for the directory root but doesn't fall back
  for arbitrary paths. A browser refresh on `/ui/jobs` (or a
  bookmark-paste of `/ui/catalog/<id>`, or any direct deep-URL
  load) hit the API surface and got `{"detail":"Not Found"}`
  instead of the SPA shell. Added a `GET /ui/{spa_path:path}`
  fallback in `app.py` that serves the real file when it exists
  under the dist tree and otherwise returns `index.html` so
  SvelteKit's client router can resolve the route. Path-traversal
  guarded via `.resolve().relative_to(dist.resolve())`.
- **`med api token create --namespace` ignored
  `MEDIA_ENGINE_NAMESPACE`.** The CLI option defaulted to the
  literal string `"default"` rather than `EngineConfig().namespace`,
  so minting a token inside an env-set namespace silently produced
  a `default`-scoped token that 403'd against the real engine
  process. The screenshot script now passes `--namespace
  "${MEDIA_ENGINE_NAMESPACE}"` explicitly; a permanent fix lands
  in the post-50 audit pass.

### Notes

- **Phase 7 (acoustic speaker identity)** queues next — see plan
  §12.6: `speakers.embed_voice` / `speakers.cluster` / `speakers.match`
  + `SpeakerEmbedding` + `SpeakerProfile` artifact kinds, stable
  `Speaker_<sha8>` ids across recordings, voice-fingerprint DB
  reusing the pgvector backend.

## [0.5.0] — 2026-05-22

Phase 5 closes the v0.5.0 release: a working starter analysis flow on
top of the primitives + transports shipped in 0.4.x.

### Added

- **ops:**
  - `speakers.identify` — rapidfuzz fuzzy-match of diarization clusters
    against a name-CSV. Pure Python, no ML deps. Cache invalidates on
    CSV content change via a `speaker_db_sha` field.
  - `report.session` — `SessionAnalysis` → `MarkdownArtifact` via a
    Jinja2 template. Cache invalidates on template-content change via a
    `template_sha` field.
  - `report.zeitgeist` — variadic `list[SessionAnalysis]` →
    `MarkdownArtifact`. Cross-session aggregation (top topics, entities,
    claims, speakers; average sentiment polarity) precomputed for the
    template.
- **profiles:**
  - `profiles/analysis-full/` — bundled end-to-end pipeline (acquire →
    extract_audio → transcribe_diarized → speakers.identify →
    intelligence.analyze → report.session). Generic, content-neutral
    schema (`summary`, `topics`, `entities`, `claims`,
    `sentiment{polarity,confidence}`, `questions`); clone & edit to
    specialize. Default LLM is local mlx-lm Qwen2.5-14B; swap via the
    `model` param.
  - Five starter `kind: prompt` profiles under `profiles/*.md`:
    `video-knowledge`, `technical-academic`, `diy-electronics`,
    `cooking-recipes`, `general-custom`. Each wraps `video.multimodal`
    with a system prompt; default backend gemini.
- **`intelligence.analyze` now accepts `prompt_path: Path`** — read at
  validation time; the resolved text replaces `prompt` in canonical
  params so editing the `.md` invalidates cache on next run.
- **Docs:** `docs/cli_reference.md`, `docs/api_reference.md`,
  `docs/adding_a_backend.md`, `docs/profile_analysis_full.md`,
  `docs/quickstart.html` (executive overview).
- **Examples:** `examples/analysis_full_pipeline_e2e.sh`,
  `examples/README.md`.
- **Scripts:** `scripts/gen_openapi.py`, `scripts/gen_mcp_tools.py` for
  regenerating the committed `docs/openapi.json` and `docs/mcp_tools.json`
  schema artifacts.

### Changed

- README rewrite — elevator pitch + 30-second tour + doc pointer table.
- `pyproject.toml::version` and `media_engine.__version__` → `0.5.0`.
- `docs/architecture.md` §11 records Phase 5 deviations.

### Fixed (post-release Phase 4 audit, same release window)

A second-pass audit of the Phase 4 surface (commits 29–34, shipped
before v0.5.0) caught one production-blocker, two real bugs, and
seven robustness improvements that lived in user-facing endpoints:

- **`infra/docker/Dockerfile` referenced a nonexistent `alembic/`
  directory** via `COPY alembic ./alembic`. Phase 4 commit 31 moved
  the migrations inside the package (`media_engine/_alembic/`), but
  the Dockerfile was never updated. Docker would have failed at build
  time. Removed the stray COPY (the in-process alembic loader at
  `cli/db.py:_alembic_config` already pins ``script_location`` at the
  packaged dir).
- **`cancel_job` could overwrite a terminal status with `cancelled`.**
  Race window: task naturally completed during the `await task`, the
  runner's `finally` wrote `completed`/`failed`, then `cancel_job`
  blindly wrote `cancelled` on top. Now checks
  `task.cancelled()` after the await and only flips to `cancelled`
  when cancellation actually took effect; otherwise returns `False`
  to the caller.
- **`runtime/eviction.py` deleted `cached_operation_runs` rows
  without a namespace filter.** The artifact id is the primary key
  so cross-tenant collisions are blocked at insert time, but the
  defense-in-depth filter is now applied — keeps the query intent
  explicit and improves the SQL plan.

### Improved (post-release Phase 4 audit)

- **FastAPI app `version` sources `media_engine.__version__`.** It was
  hardcoded `0.1.0` since Phase 4; `docs/openapi.json` + the live
  `/openapi.json` now correctly track 0.5.0.
- **SQLite search-backend metadata parity.** `search.semantic` and
  `search.fulltext` Analysis payloads now include `"backend":
  BACKEND_NAME` to match what the Postgres backends already emitted
  — consumers (reports, dashboards) see a consistent shape across
  drivers.
- **Bearer-token parsing tolerates extra whitespace.** `Authorization:
  Bearer  <token>` (double space) used to 401 silently because the
  leading space ended up in the hash lookup; now stripped after the
  partition.
- **`runtime/health.py::_check_storage_writable` writes a probe file.**
  `os.access(..., os.W_OK)` lies on read-only mounts, exhausted
  inodes, and ACL overrides; only a real round-trip is honest.
- **Readiness probe gates on `min_free_gb`.** Free disk space is now
  a first-class readiness check — kubelet pulls a pod out of traffic
  when the engine's disk-guard would have started failing writes.
  Reports `degraded` between threshold and 2× threshold.
- **`periodic_workdir_gc` logs swept errors instead of swallowing them.**
  Failed sweeps used to disappear into `contextlib.suppress(Exception)`;
  now they log at WARNING with traceback so operators can see when
  GC stops working.
- **`runtime/resources.py` rejects unknown keys in a resource body.**
  A typo like `capcity: 1` used to silently fall through to
  `capacity=1` (default); now raises `ResourcesConfigError` with the
  bad key + the allowed set.
- **`med storage migrate` validates the `--to` path before rewriting
  cache rows.** A typo used to point every row at a nonexistent
  directory; now fails fast with a clear error and untouched cache.
- **SSE pumper awaited on disconnect.** `job_event_stream`'s cleanup
  now waits for the pumper task to finish cancelling so the underlying
  `bus.subscribe()` generator's finally-clause (unregisters from
  `EventBus`) runs before the request returns — keeps the subscriber
  list clean under heavy SSE churn.
- **`speakers.identify` builds `speaker_extra` in O(N+M).** The
  payload's `canonical → extra_columns` map used to come from a
  quadratic nested generator (scanned the whole db per match). Now
  goes through a single-pass `_collect_extra` helper that indexes
  the db once.
- **Content-addressed cache key for path fields.**
  `IdentifyParams.speaker_db`, `SessionReportParams.template`, and
  `ZeitgeistReportParams.template` are now `Field(exclude=True)` so
  the cache key tracks the file's sha (via the auto-derived `*_sha`
  fields) rather than its filesystem path. Two callers referencing
  the same file by different paths (e.g. relative vs absolute) now
  hit the cache. New regression tests cover both same-content-
  different-path cache hits and same-path-different-content cache
  misses.
- **Auto-derived `*_sha` fields marked `readOnly` in their JSON
  schemas + carry a description.** Pydantic `model_dump_exclude` only
  affects serialization, not the schema, so MCP and REST surfaces
  still advertised `speaker_db_sha` / `template_sha` as settable
  string fields. Clients (LLMs driving the MCP tools, the Phase 6
  Web UI form generator) now see `{readOnly: true, description:
  "Auto-derived sha … clients should not set this …"}` and can
  hide or disable the field. The validator continues to overwrite
  any client-supplied value, so the change is purely UX.

### Notes

- **`speakers.identify` is name-CSV only** in Phase 5. Acoustic identity
  (`speakers.embed_voice` / `cluster` / `match`) is Phase 7 — see
  plan §12.6.
- **Phase 6 (local-first Web UI)** queues next; see plan §12.5.
- One ratified deviation: `speakers.identify` operates on Transcript →
  Transcript (not Diarization → Diarization as the plan text said). The
  as-built `Diarization` artifact has no per-segment text — text lives
  in the `Transcript` produced by `audio.transcribe_diarized`, so that
  is what the op consumes.

## [0.4.0] and earlier

Phases 0–4 landed without per-version tagging during pre-release. See
plan §0 (`~/.claude/plans/goofy-gathering-beaver.md`) and
`docs/architecture.md` §11 for the per-phase commit list. Summary:

- **Phase 0 (commits 1–4):** typed artifacts, Engine + cache + storage,
  `med` CLI scaffold, ffmpeg/ffprobe wrapping.
- **Phase 1 (commits 5–12):** DAG executor, `audio.diarize`,
  `audio.transcribe`, `audio.transcribe_diarized`, frames.* family,
  intelligence.* family, profile system, daemon.
- **Phase 2 (commits 13–20):** `video.multimodal`, `image.*` family,
  `chunk.semantic`, `embed.text`, `transcript.*`, MCP exporter
  skeleton, retry + events + cost ledger.
- **Phase 3 (commits 21–28):** `acquire.url` + `acquire.livestream`,
  `web.fetch`, `document.parse`, `metadata.scrape_page`,
  `search.fulltext` + `semantic` + `hybrid`, lineage hardening.
- **Phase 4 (commits 29–34):** FastAPI REST + bearer tokens + Job
  concept + SSE; full MCP stdio server with allow-list; Postgres /
  pgvector / postgres-tsvector backends + alembic migrations + `med
  db`; LRU eviction + workdir GC + `med storage`; IaaC bundle
  (Dockerfile, docker-compose, Helm, Terraform) + `/health` + `/ready`
  + `med health` / `ready`; `resources.yaml` declarative resource
  overrides.
