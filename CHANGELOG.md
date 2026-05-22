# Changelog

All notable changes to `media_engine` are tracked here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semver applies
once we ship v1.0 (after Phase 6 — the REST surface needs to freeze
first). Until then expect 0.x to bump frequently and best-effort
backwards compatibility.

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
