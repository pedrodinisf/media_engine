# Changelog

All notable changes to `media_engine` are tracked here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semver applies
once we ship v1.0 (after Phase 6 — the REST surface needs to freeze
first). Until then expect 0.x to bump frequently and best-effort
backwards compatibility.

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
