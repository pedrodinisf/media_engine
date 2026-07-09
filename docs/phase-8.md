# Phase 8 — Profile transparency, pre-run validation & config editor

Bumps the engine to **v0.9.0**. Two related web-UX shipments plus the engine
plumbing that backs them.

The trigger: operators running bundled **profiles** (pipeline templates) from
the Web UI couldn't tell what a profile actually *does* — which models it uses,
whether a node runs on a **cloud API** or a **local model**, or that a required
setting was missing — until a run failed. The canonical example:
`video.comprehend` failing at run time with
`fps × duration (3897 frames) exceeds max_frames (240)`, only visible in the
Job-failed view *after* submitting.

## 1. Feasibility is declared by ops and surfaced before execution

A new engine principle: **ops declare pre-run feasibility, and the engine
surfaces it everywhere before running.**

- `Operation.validate_params(inputs, params)` — optional hook (default no-op).
  Raises `ValueError` with a human-actionable message when an (inputs, params)
  pair can't succeed. Must be side-effect-free and host-independent (it runs
  speculatively in API/preview processes). `video.comprehend` implements it by
  factoring its frame-budget check into a shared `_check_frame_budget` that both
  `run()` (backstop) and `validate_params()` (preflight) call.
- `Engine.preview_pipeline(pipeline) -> list[NodeCostPreview]` — generalizes the
  previously-unused `estimate_pipeline_cost` into a per-node preflight: resolved
  backend, cached flag, `resolvable` (all inputs are pipeline sources?), cost,
  and `feasibility_error` (from `validate_params` or a B-008 backend/model
  conflict). Source-fed nodes are checked; downstream nodes whose inputs are
  upstream outputs report `resolvable=false` ("not preflighted", ≠ "OK").
- Surfaces:
  - `POST /pipelines/preview` — the workspace **Run** button calls this first
    with the picked sources, renders per-node cost + feasibility, and **blocks
    submission** on any `feasibility_error`. Compile/load failures return
    `ok=false` + a typed envelope (200, mirroring `/profiles/validate`).
  - `POST /run/preview` gains a `feasibility_error` field (single-op sibling).
  - `med profile run --dry-run` prints a per-node preflight table + DAG total and
    exits non-zero if any node is infeasible; the per-op `med run --dry-run`
    threads `validate_params` too.

## 2. Model / backend transparency

Static introspection (`media_engine/profiles/introspect.py`) — pure analysis over
`OpRegistry` + `BackendRegistry` + the params JSON Schema, safe to run per
keystroke:

- `POST /profiles/validate` `compiled_nodes` are enriched (additively) with
  `resolved_backend`, `provider` (`cloud`/`local`/`composite`/`unknown`),
  `models` (each model-typed param + its value + provider), and a
  `requirement_hint` (e.g. `needs GEMINI_API_KEY`).
- `GET /profiles` summaries gain a `digest` (distinct models + providers +
  requirement hints) so the list cards can show
  `gemini-2.5-pro (cloud) + Qwen2-VL (local) · needs GEMINI_API_KEY`.
- Cloud-vs-local is classified in `runtime/doctor.py::classify_provider` — a
  backend needing an `*_API_KEY` env is cloud; hardware/binaries/RAM is local.
  The `_API_KEY` suffix check is load-bearing so `HF_TOKEN` (pyannote) doesn't
  mis-tag a local backend as cloud. The doctor report now carries `provider`.
- Model fields are detected by the `(^|_)model$` name pattern (NOT enum
  presence — `style`/`output_kind` carry enums but aren't models). `vlm_model`
  and several free-text `model` params gained curated enum dropdowns
  (`ops/video/_models.py`, `ops/embed/_models.py`).

## 3. Web UI

- **Profiles list** cards render model/provider badges + requirement hints.
- **Profile workspace** (`/ui/profiles/[name]`):
  - Un-defers the **per-node param editor** — the deferred YAML-only note is
    replaced by a schema-driven form (reuses `SchemaForm`), fetched from
    `GET /operations/{name}`. Edits round-trip through the YAML AST via a new
    `mutateNodeParams` that omits values equal to their default (minimal YAML)
    and preserves comments/order. Seeded off `selectedNodeId`/op only (never
    per-keystroke) so the 150 ms layout debounce can't reset the form mid-edit.
  - Each op node + the per-node panel show a cloud/local/composite provider
    chip, the resolved backend, model name(s), and any requirement hint. A
    header **summary strip** aggregates the whole profile's models + gaps.
  - **Run** now opens a **preflight panel** (per-node cost + feasibility) and
    blocks Submit on infeasible configs.
- **`ModelSelect`** — a provider-grouped model dropdown (Local / Cloud
  `<optgroup>`s + a cloud/local badge), routed in from `SchemaForm` for any
  model-typed enum field, so it lands in both the workspace and the `/run`
  panel. Off-list custom ids (set via YAML) are preserved.

## 4. Config editor

Settings → Config becomes **editable** (was read-only):

- `PUT /settings/config-files` writes `config.toml` and/or `resources.yaml`.
  Validates before writing (TOML parse + `EngineConfig` round-trip + unknown-key
  rejection; YAML parse + `resources` round-trip + unknown-op rejection); returns
  422 with the message and writes nothing on invalid input. **`secrets.env` is
  structurally unwritable** here — it keeps its own masked `PUT /settings/secrets`
  flow.
- The Config tab gets editors (CodeMirror for `resources.yaml`, textarea for
  `config.toml`), Save + inline validation-error surface, create-when-missing,
  Reload-from-disk, and — because config is read once at boot — a prominent
  **restart notice** on save.

## Out of scope
- GUI run for prompt (`.md`) profiles is unchanged (still CLI / `/run`); they
  do get the new card digest.
- `resources.yaml` isn't hot-applied — semaphores are built at session open, so
  the restart notice is the honest UX.

## Tests
- Python: `tests/test_profile_introspect.py`, `tests/test_pipeline_preview.py`,
  enriched `tests/test_api_profiles.py`, `tests/test_api_settings.py` (PUT
  config-files).
- Web unit: `web/tests/unit/profile-params.test.ts` (classifier +
  `mutateNodeParams`), extended `settings.test.ts`.
- Operator e2e: `web/tests/e2e/flows/profiles_preflight.spec.ts` +
  config-editor specs in `settings_and_b005.spec.ts`.
