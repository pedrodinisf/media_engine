# Phase 6.7 — live observability + `video.comprehend`

Mid-cycle release that sits between Phase 6.6 (audit + close-out of
v0.6.2) and Phase 7 (acoustic speaker identity). Bumps the engine to
**v0.7.0**.

Two bundled shipments because the second leans on the first for
debugging UX:

1. **Live observability surface** — heartbeat-driven `Progress`
   events with RAM / ETA / pool telemetry, and the previously-dead
   `LogLine` event wired through five real producers (ffmpeg ×2,
   mlx-whisper, pyannote, vllm-mlx server). Web UI Logs tab + status-
   header gauges consume both.
2. **`video.comprehend` composite op** — Video → per-frame VLM +
   diarized transcript → time-fused MarkdownArtifact → one SOTA-LLM
   call → structured (or prose) Analysis.

This file is the canonical ledger for the phase. Architecture details
live in `docs/architecture.md` §12 (observability) + §13 (comprehend).
Web UI changes are walked through in `docs/web_ui.md` §4.3.

---

## Shipped (commits, newest first)

| Commit | Surface |
|---|---|
| `docs(profiles): video-comprehend.yaml example` | Phase B.5 — single-node DAG profile operators can paste into the Profiles workspace and run. |
| `test(ops/video): video.comprehend unit specs` | Phase B.4 — 10 unit tests covering fan-out arity, timeline ordering, output_kind routing, derived-id determinism, hardware gate. |
| `feat(ops/video): video.comprehend composite op` | Phase B.2 — the op + `_comprehend_prompts.py`. Registered in `bootstrap.py`. |
| `feat(backends/video_multimodal): export release_server() for vllm-mlx` | Phase B.3 — refactor-only export so `video.comprehend` can free the server's ~8-12 GB between phases. |
| `feat(ops/audio): release_audio_models helper also drops pyannote from model_pool` | Phase B.1 — rename + extend `_release_transcribe_model_cache` (now public) to also sweep `pyannote:*` from `ctx.model_pool`. |
| `test(observability): e2e — heartbeat + log_pump fire during Engine.run` | Phase A.5 — synthetic op proves the wiring carries through `Engine.run`. |
| `feat(web): Logs tab + live RAM/ETA gauges on Job detail` | Phase A.4 — Job-detail page grows the new tab + header pills + dedicated 2000-entry log buffer + auto-scroll. |
| `feat(backends): emit LogLine from ffmpeg / vllm-mlx / mlx-whisper / pyannote` | Phase A.3b — wires the four log_pump emitters into the load-bearing native backends. |
| `feat(runtime): log_pump module — subprocess stdout/stderr + Python loggers → LogLine` | Phase A.3a — public `attach_subprocess`, `attach_logger`, `attach_file_tail`, `LinePump`. 5000-line cap per (source, op_run). |
| `feat(runtime): heartbeat task emits RAM + ETA every 2s during Engine.run` | Phase A.2 — new `runtime/heartbeat.py` + engine wire-in. `cost_estimate` moved to pre-run so the heartbeat has an initial ETA. |
| `feat(runtime/events): Progress carries available_memory_gb + eta_seconds + pool_bytes_estimate` | Phase A.1 — three optional fields on the existing `Progress` model. |

Audit-pass cleanups (squashed into a small number of fix commits;
see `git log` for the full set):

* **ffmpeg failure path** — stderr tail (last 20 lines) included in
  the RuntimeError so CLI users without the Logs tab still get
  diagnostic context.
* **vllm-mlx boot output** — `attach_file_tail` moved before
  `_ensure_server` so the 30-60 s boot phase is visible.
* **`MlxWhisperDetectLanguageBackend`** — also gets the logger
  bridge (was missed in the initial wire-in).
* **`attach_file_tail` truncation** — detects shrink (server
  restart / log rotation) and resets offset to 0.
* **Logs tab auto-scroll** — scrolls to bottom on new entries
  unless the user scrolled up. `tail ↓` button to resume.
* **Separate logs buffer** — 2000-entry, distinct from the 500-event
  general SSE tail.
* **`video.comprehend` empty-timeline guard** — refuses to call the
  synth model when both modalities produced nothing.
* **`OperationContext.job_id` + `op_run_id`** — backend Progress +
  LogLine emitters previously emitted with `job_id=None`, which the
  per-job SSE filter dropped. Plumbed through the ctx + propagated
  to every emit site. This was a pre-Phase-A latent bug, but the
  Phase A.4 Logs tab is what surfaced it.

* **`pyannote.audio` 4.x `DiarizeOutput` unwrap** — pyannote 4.x
  wraps the `Annotation` in a `DiarizeOutput` dataclass instead of
  returning it directly. The diarize backend now detects the wrapper
  and drills into `.speaker_diarization`; 3.x callers stay on the
  passthrough path. Surfaced by the first real end-to-end run of
  `video.comprehend` through the Web UI.

* **Time-window slicing across the ops that take Audio / Video.**
  `start_s` / `end_s` consistently mean "extract / process only the
  segment between these source-video seconds." Coverage as of the
  Phase 6.7 close:

  | Op | Status |
  |---|---|
  | `audio.transcribe` | ✓ already supported |
  | `audio.diarize` | ✓ already supported |
  | `audio.transcribe_diarized` | ✓ already supported |
  | `audio.detect_language` | ✓ added in Phase 6.7 |
  | `video.extract_audio` | ✓ added in Phase 6.7 |
  | `video.sample_frames` | ✓ added in Phase 6.7 (`ffmpeg-uniform` only — `pyscenedetect` refuses with NotImplementedError until follow-up) |
  | `video.comprehend` | ✓ added in Phase 6.7; forwards into all sub-ops |
  | `video.trim` | (windowing is the op's entire purpose) |
  | `video.multimodal` | ⏸ deferred — workaround: pipeline `video.trim` → `video.multimodal`. Both backends would need backend-specific slicing (gemini uploads the full file; vllm-mlx already delegates frame extraction to `video.sample_frames` which now supports it). |

  The Web UI Run-panel range slider was generalised from an audio-
  only allowlist to a schema-driven detector (`params_schema` has
  both `start_s` + `end_s` AND the first input artifact carries
  `metadata.duration`), so every op in the ✓ rows above lights up
  the slider automatically.

---

## Quality gates

Final counts after the phase:

| Gate | Baseline (v0.6.2) | After Phase 6.7 |
|---|---|---|
| `uv run pytest -q` | 1012 pass / 6 skip | **1035 pass / 6 skip** (+23) |
| `uv run ruff check` | clean | clean |
| `uv run pyright media_engine` | 0 errors | 0 errors |
| `pnpm -C web typecheck` | 0 errors, 581 files | 0 errors, 581 files |
| `pnpm -C web test` | 70 | 70 |
| `bash scripts/verify_b001.sh` | 1 / 1 | 1 / 1 |
| `bash scripts/verify_settings.sh` | 14 / 14 | 14 / 14 |
| `bash scripts/verify_observability.sh` | — (new) | **3 / 3** |
| `uv run python scripts/op_matrix.py` | ✓ 20 · ⊘ 14 · ✗ 0 | ✓ 20 · ⊘ 15 · ✗ 0 (+1 ⊘ — `video.comprehend` skipped without `transcribe-mlx` + `diarize` extras) |

---

## Plan deviations

The plan at
`~/.claude/plans/could-i-combine-the-magical-waterfall.md` claimed
`backends/video_multimodal/vllm_mlx.py:105+` contained a
`subprocess.Popen(...)` to replace with `asyncio.create_subprocess_exec`.
In reality, the vllm-mlx server is owned by `ServerManager.start()`
(detached process writing to a log file so it survives across CLI
invocations). Replacing that with PIPE'd asyncio would break the
detached lifecycle. The actual fix: `attach_file_tail()` against
`sm.log_path(_SERVER_NAME)` from within the backend's `execute()`.
Documented in `docs/architecture.md` §12.

---

## What's NOT in this phase

* Linux vllm-mlx alternative (OpenAI-compatible local VLM backend
  for non-Apple-Silicon hosts). Deferred — the hardware gate at
  `video.comprehend.run()` raises with a clear pointer.
* `frames.analyze_each` standalone op. Deferred until a second
  caller materialises; Phase B does fan-out inline.
* Dynamic concurrency tuning. `max_concurrent_frames` is a static
  param; no adaptive ramp-up.
* Custom `FramewiseAnalysis` artifact kind. Defer until cross-DAG
  sharing makes it worth it.
* New CLI verb dedicated to `video.comprehend`. Use the generic
  `med run video.comprehend --param …` for now.
