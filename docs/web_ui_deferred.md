# Web UI — Deferred scope (v1.x backlog)

Items consciously excluded from Phase 6 v1 (commits 39–50). Each one
has a recorded reason for the deferral and a sketch of what bringing it
in would entail. Phase 7 (acoustic speaker identity) has since shipped
(v0.8.0) — its *engine* side is done; only the Web-UI surface for it
(below) remains deferred.

Source of truth: this file. The plan file
(`~/.claude/plans/you-are-resuming-goofy-spark.md` §10) and the project
memory (`memory/web_ui_v1_deferred.md`) reference here.

---

## Streaming + protocol

### WebSocket protocol
- **Status:** out
- **Why:** SSE covers every Phase-6 use case (job progress, global
  event tail, search-as-you-type). Adding a second streaming surface
  doubles auth/reconnect/back-pressure code without a concrete consumer.
- **Bring-in trigger:** a feature needs bidirectional streaming
  (collaborative editing of a profile? live VLM inference with browser-
  side feedback?). Until then, every realtime path stays on
  `sse-starlette` + `EventSource`.

---

## Artifact previews

### Wavesurfer.js audio waveform
- **Status:** out
- **Why:** v1 uses `<audio controls>` which gives play/pause/seek for
  free. wavesurfer adds ~150 KB gz + a Svelte wrapper for a feature
  most users don't actually need.
- **Bring-in trigger:** a profile workflow that needs to mark segments
  by hand (e.g. manual diarization correction). Until then, `<audio>`
  + the transcript timeline view in `ArtifactPreview.svelte` covers
  the discovery use case.

### OCR bounding-box overlays
- **Status:** out
- **Why:** `OCRText.text` is the canonical artifact field; rendering
  boxes needs the source image + per-word geometry the v1 schema
  doesn't surface uniformly across `rapidocr` and `gemini-vision`
  backends.
- **Bring-in trigger:** a profile renders OCR for visual document
  workflows. Implementation: `OCRText.metadata.boxes` (list of
  `{text, x, y, w, h}`), an `<svg>` overlay positioned over the
  source `Image` in `ArtifactPreview.svelte`. Backend change first
  (both OCR backends emit the geometry), then UI follows.

### t-SNE / UMAP embedding projection
- **Status:** out
- **Why:** Pulls a sklearn-style dim-reduction lib into the bundle for
  a visual that's only meaningful at >100 embeddings. The
  `Embedding.svelte` stats card (dim + source) is the v1 affordance.
- **Bring-in trigger:** a profile produces enough embeddings to make a
  2D scatter useful (e.g. cross-document `embed.text` clustering).
  Likely UMAP via WebAssembly (`umap-js`) computed client-side off the
  embeddings list, with a Layer Chart scatter plot.

### PDF / Document advanced rendering
- **Status:** plain-text fallback shipped; pdf.js deferred
- **Why:** pdf.js needs `wasm-unsafe-eval` in CSP (already provisioned
  in `media_engine/api/middleware.py`) and adds ~2 MB to the bundle.
  v1 ships `Document.text` extracted by `pymupdf` as `<pre>`, which
  covers search-and-skim. Real PDF viewer can land later.
- **Bring-in trigger:** users complain that the extracted text loses
  structure (tables, columns). Lazy-load pdf.js when `kind=document`
  and the source has the original bytes available via
  `/artifacts/{id}/file`.

---

## Settings + ops

### In-UI `config.toml` + `resources.yaml` editor
- **Status:** read-only view in v1 (commit 49)
- **Why:** `MEDIA_ENGINE_*` env vars are owned by the deploy
  (Dockerfile / Helm values / compose); editing them from a running
  process is a footgun — the env doesn't refresh, and a bad edit
  bricks the next boot. Same logic for `resources.yaml`.
- **Bring-in trigger:** a clear UX win for "I have one machine and
  want to tweak `eviction_max_gb` without dropping to a shell."
  Implementation: dedicated PATCH endpoint that writes to
  `{config_dir}/config.toml` (not env), validates against the
  `EngineConfig` Pydantic model, and shows a "restart engine to
  apply" notice. `resources.yaml` is easier — it's already reloaded
  by the loader.

### Daemon lifecycle UI (`med daemon start/stop/status/logs`)
- **Status:** out
- **Why:** The daemon is a power-user feature for sub-second CLI
  startup. Web users hit the API process, not the daemon. Exposing
  daemon start/stop from a UI hosted *by* a non-daemon API process
  doesn't compose cleanly.
- **Bring-in trigger:** unclear. The CLI is the right surface; the
  parity matrix in plan §6 already exempts daemon commands.

### `med db migrate` + `med storage migrate`
- **Status:** out
- **Why:** Both are operator commands that touch shared infra
  (Postgres schema, on-disk artifact tree). Running them through a
  web request when uvicorn is mid-flight is unsafe. CLI is the right
  surface — the parity matrix exempts them.
- **Bring-in trigger:** N/A. These stay shell-only.

---

## Accessibility + reach

### Mobile / responsive layout
- **Status:** desktop-first; best-effort on smaller screens
- **Why:** v1 prioritizes the dense desktop tooling experience
  (catalog tables, profile composer, schema forms). Tailwind's
  responsive classes are wired (`sm:`, `md:`, `lg:`) so the layout
  isn't broken on narrow viewports, but tablet / phone aren't tested.
- **Bring-in trigger:** real mobile usage. Adds: hamburger nav,
  collapsing tab bars on `/ui/run` + `/ui/jobs/[id]`, a phone-
  friendly catalog list view.

### i18n
- **Status:** out
- **Why:** Single-developer-first tool; the engine doesn't localize
  either. Strings are inline-EN throughout.
- **Bring-in trigger:** an actual non-English user. `svelte-i18n` or
  `@sveltejs/messageformat` would slot in cleanly; the string surface
  is small (~80 keys).

---

## Phase 7 hooks

### Acoustic speaker identity UI (`speakers.embed_voice`, `cluster`,
### `match`, `SpeakerEmbedding` + `SpeakerProfile`)
- **Status:** UI still out — but the trigger has fired (Phase 7 shipped
  in v0.8.0). The ops (`speakers.embed_voice` / `cluster` / `match`),
  backends, and `SpeakerEmbedding` / `SpeakerProfile` kinds now exist and
  are usable via CLI (`med speakers …`) + REST; only the dedicated Web-UI
  surface remains deferred.
- **Why:** Phase 6 shipped zero speaker-DB UI beyond what the
  schema-driven form renders for the existing `speakers.identify` (the
  name-CSV fuzzy match from Phase 5). The acoustic ops + new artifact
  kinds landed in Phase 7 without bespoke UI.
- **Bring-in trigger:** fired. Remaining UI work to add: `SpeakerEmbedding`
  + `SpeakerProfile` preview components (likely a centroid + members
  list), a `/ui/speakers` route for cross-recording cluster browsing
  + `display_name` editing, a privacy-respecting purge button (the
  engine's `med speakers purge` / `Cache.purge_namespace` already exists).

---

## Hardening paths (from plan §13)

These aren't "features" but security tradeoffs we explicitly took for
v1 with documented v1.x paths.

### `?token=` SSE query-param leakage
- **Why deferred:** EventSource can't set custom headers; v1 accepts
  the secret in the URL on loopback/private-network deploys.
- **v1.x path:** `POST /jobs/{id}/event-token` returns a short-lived
  (~30 min) job-scoped nonce; `EventSource("…?nonce=…")` verifies
  against an in-memory map keyed by `(job_id, expires_at)`. Original
  `?token=` query stays for backwards compat.

### Token in localStorage XSS exposure
- **Why deferred:** single-origin UI, no third-party scripts, CSP
  `default-src 'self'` shrinks the XSS surface.
- **v1.x path:** httpOnly cookie session minted by
  `POST /sessions { bearer }`. SSE then rides on the cookie. Adds
  CSRF surface; needs double-submit token.

### Plugin catalog gate is filter-only
- **Why deferred:** v1 hides via `OpRegistry.list_visible()` /
  `BackendRegistry.for_op_visible()` at list-time. A motivated MCP
  client could still call `tools/list` against an unfiltered server
  if the gate file is wrong.
- **v1.x path:** enforce inside the engine — `OpRegistry.get(name)`
  raises if hidden, with an override flag for tests.

---

## Cross-reference

- Master plan: `~/.claude/plans/goofy-gathering-beaver.md` §12.5
- Phase 6 plan: `~/.claude/plans/you-are-resuming-goofy-spark.md` §10
  + §13
- Project memory: `memory/web_ui_v1_deferred.md`
- Architecture deviations: `docs/architecture.md` §11 (post-commit-50
  sync)

When closing one of these items, delete the corresponding section here
and note the close in `CHANGELOG.md` against the version it ships in.
