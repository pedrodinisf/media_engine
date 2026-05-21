# Adding an Operation

An operation (Op) is a typed verb the engine speaks. Every Op turns one or
more typed input artifacts into one or more typed output artifacts. The
engine handles caching, lineage, retry, resource semaphores, and event
emission; the Op only describes "what" — not "how to dispatch."

## 1. Pick a name

Names follow `<group>.<verb>` (lowercase, snake_case allowed inside each
half, single dot). The `group` is the **capability domain** (`audio`,
`video`, `frames`, `intelligence`, `embed`, …) — never a technology
(`mlx_whisper.transcribe` ❌). Examples:

```
audio.transcribe
video.extract_audio
frames.subsample
intelligence.summarize
```

## 2. Decide whether you need a Backend layer

If your Op has exactly one implementation, embed the logic directly in
`Operation.run()`. `acquire.upload` and `video.extract_audio` are like this.

If your Op has (or will have) multiple implementations — local vs cloud,
mlx vs gemini vs claude — split logic into a `Backend` and have the Op
delegate via `BackendRegistry.get(self.name, backend_name).execute(...)`.
`audio.transcribe` (mlx-whisper / openai-whisper / gemini-inline) and
`video.multimodal` (gemini / vllm-mlx) are this shape.

The cache key includes `(backend.name, backend.version)`, so swapping
backends produces a new artifact id.

## 3. Create the file

```text
media_engine/ops/<group>/<verb>.py
```

(Make sure `media_engine/ops/<group>/__init__.py` exists; can be empty.)

Skeleton:

```python
"""``<group>.<verb>`` — one-sentence description (used by med ops + MCP)."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Audio,           # input kind
    Kind,
    Transcript,      # output kind
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)


class MyOpParams(BaseModel):
    sample_rate: int = 16000
    # ...


@register_op
class MyOp(Operation):
    """One-line user-facing description (rendered by med ops + MCP)."""

    name = "<group>.<verb>"
    version = "1.0.0"
    input_kinds = (Kind.Audio,)            # or () for nullary ops
    output_kinds = (Kind.Transcript,)
    params_model = MyOpParams
    declared_resources = ("apple_gpu",)    # or () if no resource lock
    default_backend = "mlx-whisper"        # None when logic embedded in Op

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, MyOpParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Audio):
            raise ValueError(
                f"my.op expects exactly one Audio input, got {[a.kind for a in inputs]}"
            )
        audio: Audio = inputs[0]

        # ... do the work or delegate to backend ...

        derived_id = compute_derived_artifact_id(
            kind=Kind.Transcript,
            op_name=self.name,
            op_version=self.version,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[audio.id],
        )
        # Stage the output to ctx.workdir, then store via ctx.storage.
        out_path = ctx.storage.artifact_path(derived_id, ".json")
        # ...

        return [
            Transcript(
                id=derived_id,
                path=out_path,
                metadata={"text": "..."},
                derived_from=(audio.id,),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        audio = inputs[0]
        if isinstance(audio, Audio) and audio.duration is not None:
            return CostEstimate(local_seconds=audio.duration * 0.3)
        return CostEstimate(local_seconds=10.0)
```

## 4. Use the OperationContext

| Field             | Use it for                                                      |
| ----------------- | --------------------------------------------------------------- |
| `workdir`         | Per-job tmp dir — stage outputs here, then `storage.store_file` |
| `config`          | Read `ffmpeg_path`, `permanent_store`, `namespace`, etc.        |
| `storage`         | `artifact_path(sha, ext)` + `store_file(src, sha, ext, mode)`   |
| `emit`            | `Progress` / `LogLine` events for real-time UI updates           |
| `model_pool`      | `get_or_load(key, loader)` for warm ML models                   |
| `server_manager`  | Process lifecycle for backends like vllm-mlx                    |
| `run_op`          | Composite Ops dispatch sub-ops through the same cache            |

## 5. Make the output id deterministic

Always derive output ids via `compute_derived_artifact_id` — never via
`compute_artifact_id` (sha of bytes). Ffmpeg outputs aren't byte-stable
across builds; cross-machine cache hits would otherwise fail.

For composite Ops (e.g. `audio.transcribe_diarized`), include the sub-op
output ids in `input_ids` so changes ripple into the composite id.

## 5b. Optional Operation mechanisms

Beyond the core contract, three opt-in class attributes cover common
shapes (all default to "off" — see `docs/architecture.md` §4 for the
rationale):

- `variadic_inputs = True` — the op takes one input that may be one of
  several `input_kinds`, or ≥2 inputs each of a kind set. The engine
  validates *membership* instead of a fixed positional signature; your
  `run()` enforces the exact arity. (e.g. `embed.text`,
  `frames.compare`, `intelligence.*`.)
- `def select_backend(self, params) -> str | None` — pick the backend
  from params (e.g. by model prefix). The engine's precedence is
  `explicit backend= > select_backend > default_backend`; dispatch off
  `ctx.backend` in `run()` so the backend that runs is the one recorded
  in the cache key / cost ledger / provenance. Never read a `backend`
  field off your params model — that collides with `Engine.run`'s
  reserved `backend=` kwarg.
- `records_cost = False` — for a thin composite that delegates to a
  sub-op via `ctx.run_op`; the sub-op already bills the spend, so the
  wrapper must not double-count.

## 6. Register tests

Drop `tests/test_op_<group>_<verb>.py`. Cover:

- **Op contract**: class attrs (name/version/kinds/default_backend),
  params defaults, `cost_estimate` scales sensibly.
- **Engine.run dispatch**: end-to-end via `engine.run("op.name", inputs=[id])`.
- **Cache hit on rerun**: second call doesn't re-invoke the backend
  (use `mocker.spy` on the relevant subprocess or backend method).
- **Param change → new id**: changing any param produces a new artifact id.
- **Kind validation**: passing the wrong kind raises a clear error.
- **Error paths**: missing binary, missing env var, malformed input.

If your Op uses an optional ML library, gate the integration test with
`@pytest.mark.needs_<feature>` + `pytest.importorskip(...)`.

## 7. Wire it into the catalog

Add your Op class to `media_engine/bootstrap.py::_op_classes()` (and any
new backend to `_backend_classes()` — optional-dep backends go in a
`try/except ImportError` block). `bootstrap.register_all()` is the single
catalog every transport and the test suite's autouse
`conftest.py::_ensure_registries` fixture call; without the entry your op
is invisible and registry-clearing tests won't restore it.

## 8. Verify

```bash
uv run pytest -k <verb>
uv run pyright media_engine
uv run ruff check
uv run med ops              # confirm your op shows up
uv run med mcp tools-json   # confirm the MCP schema is sensible
```

Phase 4 surfaces (REST + MCP stdio server) pick up new ops
automatically — `GET /operations` lists them, `GET /operations/<name>`
returns the params schema, and `med mcp serve --allow <name>` exposes
the op as a tool. No extra registration is needed beyond
`bootstrap._op_classes()`.

That's it. The engine handles content addressing, cache, lineage,
events, and retry around your `run()`.
