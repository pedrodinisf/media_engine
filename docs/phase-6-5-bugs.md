# Phase 6.5 — bug triage

> Surfaced during manual smoke testing of v0.6.0, the `med doctor`
> rollout, and the `scripts/op_matrix.py` run. Triage labels:
>
> - **p0** — blocks the documented happy path; needs fix this session
> - **p1** — degrades UX but a workaround exists
> - **p2** — cosmetic, polish, or a deeper redesign that should wait

Each row links to the file/line where the relevant code lives. Fixed
bugs are struck through and reference the commit that closed them.

## Open

| id | priority | bug | repro | suspected cause | fix attempt |
|----|----------|-----|-------|-----------------|-------------|
| ~~**B-001**~~ | ~~p0~~ | ~~Job-detail Events tab shows `Waiting for events…` indefinitely.~~ | Closed; see "Recently closed". | — | Two independent root causes: (1) server: `Engine.run` minted its own internal id and stamped events with it, so the REST job_id never matched the SSE filter; (2) client: SSE wrapper listened for `OpStarted/...` (PascalCase) but server emits `op_started/...` (snake_case), so even when events landed they were ignored. Fixed both + added replay-on-subscribe so events emitted before the EventSource handshake completes are also delivered. |
| **B-002** | p1 | `audio.transcribe` accepts a Video artifact id silently then errors with a confusing runtime message. | Run panel → pick `audio.transcribe` → paste a Video id (wrong kind) → Submit. The job either errors with a stack trace or hangs. | The schema form doesn't pre-validate `input_kinds`. Engine rejects later but the UI surface is poor. `web/src/components/forms/SchemaForm.svelte` for the form; `media_engine/runtime/engine.py::_validate_input_kinds` for the canonical check. | TBD |
| ~~**B-003**~~ | ~~p1~~ | ~~`med api token create --namespace` defaults to literal `"default"`.~~ | Closed; see "Recently closed". | — | `cmd_token_create` now defaults `--namespace` to `EngineConfig().namespace` (reads `MEDIA_ENGINE_NAMESPACE`). |
| **B-004** | p1 | Run panel: `Temperature: 0,2` rendered with a comma decimal separator. | Run panel → pick `intelligence.summarize` → look at the Temperature field. Reads `0,2` instead of `0.2`. | Locale leak in the schema form's number input. Likely `toLocaleString()` somewhere in `web/src/components/forms/SchemaForm.svelte` or the int/float widget. Submitting `0,2` to the API would fail JSON validation. | TBD |
| ~~**B-005**~~ | ~~p1~~ | ~~Run panel cost preview shows `backend: —` for composite ops.~~ | Closed; see "Recently closed". | — | `POST /run/preview` already correctly returned `backend: null` for embedded composite ops (they have no Backend layer); the UI was rendering `—` because it had no way to distinguish "no backend resolved" from "composite with no backend layer at all". Added `embedded: bool` to the preview response; the run-panel UI now renders "(composite — chosen at run time)" instead of `—`. |
| **B-006** | p2 | Run panel pre-populates `Model: gemini-2.5-flash` as the default. | Run panel → pick `intelligence.summarize`. The `model` field shows `gemini-2.5-flash`. | Hard-coded default in the Pydantic params model. Should verify this model id is current against the Gemini API (was renamed/replaced between Gemini 1.5 and 2.5). At minimum the default should match a real Gemini-API model id. | TBD |
| **B-007** | p1 | Composite ops with backend routers don't propagate `--backend` overrides to their delegate calls. | `med run intelligence.summarize --input <transcript-id> --backend mlx-lm` — the composite still dispatches to `intelligence.extract` with extract's *default* backend (gemini), ignoring the override. | Composite ops call `ctx.run_op("intelligence.extract", inputs=[...])` without forwarding the user's `backend=`. The composite has no awareness it's running under an override. | TBD |
| **B-008** | p1 | `frames.analyze` / `video.multimodal` routers leave model param unchanged when `--backend` overrides routing. | `med run frames.analyze --input <frameset-id> --backend vllm-mlx` — the model param stays `gemini-2.5-pro` (its default), then vllm-mlx tries to load the gemini model and fails the hardware-fit check. | Router `select_backend(params)` reads `params.model`; when an operator forces a backend incompatible with the model default, there's no validation. Either (a) require model/backend to be consistent and 400 the request, or (b) auto-pick a backend-compatible model when overridden. | TBD |
| **B-009** | p2 | `audio.transcribe_diarized` composite reports `embedded ok` in doctor even when its delegates (audio.transcribe, audio.diarize) are unavailable. | `med doctor --op audio.transcribe_diarized` → shows `status: ok` despite mlx-whisper not being installed. Run-time fails. | Composites have no Backend layer, so doctor can't introspect their dep tree. Possible fix: ops declare a `delegates_to: tuple[str, ...]` class attribute that doctor walks. | TBD |
| **B-010** | p2 | Several backend `BackendRequirements` under-declared their Python-package deps. | Closed below — historical record. | See `audit-fix` row. | Closed by the doctor/matrix commit. |

## Recently closed

| id | bug | closing commit |
|----|-----|----------------|
| **B-005** | Run panel cost preview rendered `backend: —` for composite ops (`intelligence.summarize`, `audio.transcribe_diarized`, …). The `/run/preview` endpoint correctly returned `null` for these (they have no Backend layer), but the UI had no way to distinguish "no backend resolved" from "composite — picked at runtime by the delegate." Added `embedded: bool` to `RunPreviewResponse`; the UI now renders "(composite — chosen at run time)" when set. Regression spec at `web/tests/e2e/flows/settings_and_b005.spec.ts`. | `fix(api/web): cost preview surfaces composite ops (B-005 p1)` |
| **B-001** | Job-detail Events tab showed `Waiting for events…` indefinitely. Two root causes: (a) `Engine.run` minted its own id and stamped events with it, so REST `job_id` never matched the SSE filter; (b) the web SSE wrapper listened for PascalCase event names (`OpStarted`, …) while the server emits snake_case (`op_started`, …). Plus replay-on-subscribe is now in place so events fired during the EventSource handshake are also delivered. | `fix(api/web): SSE events deliver to job-detail (B-001 p0)` |
| **B-003** | `med api token create --namespace` defaulted to literal `"default"` instead of reading `MEDIA_ENGINE_NAMESPACE`; resulting tokens 403'd on every authed endpoint when the engine was on a non-default namespace. | `fix(cli): med api token create defaults to engine namespace (B-003 p1)` |
| **B-010** | `transcribe/mlx_whisper.py`, `document/pymupdf.py`, `embed_text/sentence_transformers.py` declared `BackendRequirements()` without their `services=[...]` Python-package deps. Doctor reported them green even when missing; users hit opaque `RuntimeError: X is not installed` at run time. | Same commit as `med doctor` (declarations updated alongside doctor enhancement). |

## Out-of-scope notes

- **Kitchen-sink container**: a `Dockerfile.kitchen-sink` that bakes in
  every backend + model would eliminate the dep-mismatch class of bug
  for container users. Out of scope for this triage; tracked as a
  follow-up direction.
- **Engine auto-fallback**: when the default backend is unavailable,
  the engine could fall through to the next eligible backend that
  passes its `BackendRequirements`. This would change cache keys
  (different backend → different artifact id), so it's a real
  behaviour change — needs design conversation, not a quick fix.

## How this list is maintained

- `med doctor` and `scripts/op_matrix.py` are the structured surfaces;
  anything new they turn up gets a row here.
- The Web UI bugs (B-001 through B-006) came from a single manual
  smoke session captured in chat — re-do that session after each
  Phase-6.5 commit to keep the list accurate.
- When a fix lands, move the row to "Recently closed" with the commit
  sha, don't delete.
