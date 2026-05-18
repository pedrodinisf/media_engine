"""Shared helpers for the intelligence.* op tests (not collected as tests).

A single fake ``intelligence.extract`` backend serves all three op test
modules: it reads ``params.schema_def`` and emits a minimal JSON instance
that satisfies it, so the real ``build_extract_analysis`` parse+validate
path is exercised end to end (only the model call is faked).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Transcript
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.intelligence.extract import (
    ExtractParams,
    artifact_to_text,
    build_extract_analysis,
)
from media_engine.runtime.engine import Engine


def example_for_schema(schema: dict[str, Any]) -> Any:
    """Smallest instance that validates against our schema subset."""
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type", "object")
    if isinstance(t, list):
        t = t[0]
    if t == "object":
        props: dict[str, Any] = schema.get("properties", {}) or {}
        required = schema.get("required", list(props)) or []
        return {k: example_for_schema(props.get(k, {})) for k in required}
    if t == "array":
        item = schema.get("items")
        return [example_for_schema(item)] if isinstance(item, dict) else []
    if t == "string":
        return "x"
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    if t == "null":
        return None
    return {}


def register_fake_extract_backend() -> type[Backend]:
    """Register a fake under ('intelligence.extract', 'gemini').

    Caller is responsible for teardown via ``unregister_fake()``.
    """
    BackendRegistry.unregister("intelligence.extract", "gemini")

    @register_backend
    class _FakeExtract(Backend):
        op_name = "intelligence.extract"
        name = "gemini"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, ExtractParams)
            from media_engine.runtime.jsonschema import load_schema

            schema = load_schema(params.schema_def)
            # Touch the source so the read path is covered too.
            _ = artifact_to_text(inputs[0])
            payload = example_for_schema(schema)
            return [
                build_extract_analysis(
                    source=inputs[0],
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    raw_text=json.dumps(payload),
                    usage={"input_tokens": 500, "output_tokens": 20,
                           "cost_cents": 0.03},
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(cloud_cents=0.1)

    return _FakeExtract


def unregister_fake() -> None:
    BackendRegistry.unregister("intelligence.extract", "gemini")
    from media_engine.bootstrap import register_all

    register_all(force=True)


def make_transcript(engine: Engine, text: str = "Hello world.") -> Transcript:
    """Persist a synthetic Transcript artifact and return it."""
    tid = "t" * 64
    payload = {"text": text, "segments": [{"start": 0.0, "end": 1.0,
                                           "text": text}]}
    path = engine.storage.artifact_path(tid, ".json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload))
    t = Transcript(
        id=tid, path=path, metadata=payload, created_at=datetime.now(UTC)
    )
    engine.cache.upsert_artifact(t)
    return t


def ctx_for(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("intel-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
        run_op=engine.run,
    )
