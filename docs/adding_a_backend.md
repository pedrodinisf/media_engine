# Adding a Backend

A Backend is a swappable implementation of an Op. Ops with multiple
plausible implementations (local vs cloud, vendor A vs vendor B, model
family X vs Y) factor the logic out of `Operation.run()` into a
`Backend` subclass and let the engine pick at run time. The cache key
includes `(backend.name, backend.version)`, so swapping backends
produces a distinct artifact id — outputs are never silently mixed.

This is the companion to
[`docs/adding_an_operation.md`](adding_an_operation.md). Read that
first if you haven't already.

## 1. Pick the op + a backend name

The backend name is kebab-case and identifies the implementation, not
the op:

| Op                       | Default backend  | Other backends                |
| ------------------------ | ---------------- | ----------------------------- |
| `audio.transcribe`       | `mlx-whisper`    | (room for `openai-whisper`, `gemini-inline`) |
| `intelligence.extract`   | `gemini`         | `claude`, `mlx-lm`            |
| `video.multimodal`       | `gemini`         | `vllm-mlx`                    |
| `acquire.url`            | `yt-dlp`         | `playwright-hls`              |
| `image.classify`         | `open-clip`      | `gemini`                      |

Use the vendor / library name, not the op's domain — the goal is that
swapping backends is purely about implementation choice, not capability.

## 2. Decide where the file lives

Two layout flavors:

- **Single-verb groups** (`diarize`, `transcribe`, `chunk_semantic`) →
  `media_engine/backends/<verb>/<provider>.py`.
- **Multi-verb groups** (`intelligence_extract`, `frames_analyze`) →
  `media_engine/backends/<group>_<verb>/<provider>.py`.
- **Group-only families** (`acquire`, `document`, `web`, `search`) →
  the verb stays in the filename: `backends/acquire/ytdlp.py`,
  `backends/search/pgvector.py`.

You're not normally creating a new backend directory — you're adding a
new file under an existing one.

## 3. Skeleton

```python
"""``<op>`` — <provider> backend."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Transcript
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext


@register_backend
class MyWhizBangTranscribeBackend(Backend):
    op_name = "audio.transcribe"
    name = "whizbang"                       # kebab-case
    version = "1.0.0"                       # bumping invalidates cache
    requires = BackendRequirements(
        env=("WHIZBANG_API_KEY",),          # required env vars
        binaries=(),                        # CLI tools that must be on PATH
        services=(),                        # logical services (informational)
        hardware=(),                        # ("apple_silicon",), etc.
        min_memory_gb=4.0,
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        # Lazy-import the heavy dep inside execute() — registration must
        # stay import-clean even when the package isn't installed.
        import whizbang  # type: ignore[import-not-found]

        # ... do the work, stage to ctx.workdir, return artifacts ...

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(cloud_cents=0.1)
```

## 4. The import-cleanness rule

**Optional-dep backends must be import-clean even when their ML library
isn't installed.** Bootstrap registers every backend at process start,
and we don't want a missing dependency to silently disable the *whole*
catalog. The pattern:

1. Imports at the top of the backend file pull in only the engine's own
   modules + Pydantic + stdlib. Never `import torch`, `import openai`,
   etc. at the top.
2. The heavy library is imported inside `execute()` (or a helper called
   from `execute()`), so it's only needed when the backend actually
   runs.
3. In `bootstrap.py::_backend_classes()`, wrap the registration in a
   `try / except ImportError` block — if the file's top-level imports
   ever fail (e.g. because we added a typing-only `from torch import
   Tensor` by accident), the backend just doesn't register, and the
   user sees a clearer error from `med ops` later.

Verify with `uv pip uninstall <your-lib> && uv run med ops` — no
ImportError should reach the user.

## 5. `BackendRequirements`

Filled in by every backend; consumed by the `Backend.is_available()`
gate and surfaced in `GET /backends/{name}`. The fields are
informational + checked-where-cheap:

| Field            | What it means                                                 |
| ---------------- | ------------------------------------------------------------- |
| `env`            | Env vars that must be set (checked via `os.environ`).         |
| `binaries`       | CLI binaries that must be on `PATH` (`shutil.which`).          |
| `services`       | Logical services this backend talks to (`"vllm-mlx"`, `"gemini-api"`); informational only — not auto-pinged. |
| `hardware`       | Hardware labels: `"apple_silicon"`, `"nvidia_gpu"`, …          |
| `min_memory_gb`  | Heuristic guard — the resource manager refuses to schedule when free RAM is below this. |

## 6. Register the backend

Add it to `media_engine/bootstrap.py::_backend_classes()` — the
optional-dep block if applicable:

```python
try:
    from media_engine.backends.transcribe.whizbang import (
        WhizBangTranscribeBackend,
    )
    classes.append(WhizBangTranscribeBackend)
except ImportError:
    pass
```

## 7. Cost model

`Backend.cost_estimate()` is consulted by the engine's `--dry-run`
preview. Be honest:

- `local_seconds` — wall-clock seconds expected (used for ETAs).
- `cloud_cents` — predicted dollar-cost ÷ 100 (used by the cost ledger
  + budget gates). For free local backends, `0.0`.
- `tokens_in` / `tokens_out` — only for LLM/VLM backends (used by the
  cost ledger).

The engine records *actual* usage post-run via the backend's
`extract_invoke` return (LLMs) or a manual call to
`ctx.storage.upsert_cost(...)`. Estimates are pre-run; actuals are
post-run.

## 8. Tests

If the op has only one backend, fold the backend tests into
`tests/test_op_<group>_<verb>.py`. If there are multiple backends,
add `tests/test_backend_<group>_<verb>_<provider>.py`.

Always provide:

- A no-deps test that exercises `cost_estimate()` and class-attr
  invariants.
- An integration test gated by `@pytest.mark.needs_<feature>` +
  `pytest.importorskip("<library>")` for the actual `execute()` call.

For ops with multiple backends, the `BackendRegistry.swap_default(...)`
context manager makes backend-swap tests trivial — see
`tests/test_op_intelligence_extract.py` for the pattern.

## 9. Verify

```bash
uv run pyright media_engine
uv run ruff check
uv run pytest -k <provider>
uv run med ops                            # backend listed under the op
GET /backends/<your-backend-name>         # via REST (after med api start)
```

That's it. The cache, cost ledger, lineage, retry, and event stream
already know about the new backend — they read it off `ctx.backend`.
