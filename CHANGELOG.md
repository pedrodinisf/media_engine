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
