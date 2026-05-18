"""``intelligence.extract`` — Transcript|Markdown|Analysis → structured Analysis.

A profile supplies a prompt + a JSON schema; the backend LLM returns JSON
matching it; the engine validates and stores it as an ``Analysis`` whose
``data`` is the extracted object. Capability-named: the model family is
chosen by ``model`` prefix (``mlx-community/*`` → ``mlx-lm``, ``claude*``
→ ``claude``, else ``gemini``) — never a technology-named op.

The engine has zero domain opinions: the schema lives in the profile.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Kind,
    MarkdownArtifact,
    Transcript,
    compute_derived_artifact_id,
)
from media_engine.artifacts.analysis import SessionAnalysis
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.runtime.jsonschema import SchemaError, load_schema, validate

_INPUT_KINDS = (Kind.Transcript, Kind.MarkdownArtifact, Kind.Analysis)


class ExtractParams(BaseModel):
    prompt: str
    # Inline JSON schema, or a path to a ``.json`` schema file.
    schema_def: dict[str, Any] | str
    model: str = "gemini-2.5-flash"
    system_prompt: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096


def _default_backend_for_model(model: str) -> str:
    if model.startswith("mlx-community/"):
        return "mlx-lm"
    if model.startswith("claude"):
        return "claude"
    return "gemini"


def artifact_to_text(a: AnyArtifact) -> str:
    """Flatten a text-bearing artifact to a single prompt-able string."""
    if isinstance(a, Transcript):
        txt = a.metadata.get("text")
        if isinstance(txt, str) and txt.strip():
            return txt
        return "\n".join(
            str(s.get("text", "")) for s in a.metadata.get("segments", [])
        )
    if isinstance(a, MarkdownArtifact):
        return a.path.read_text()
    if isinstance(a, (Analysis, SessionAnalysis)):
        data = a.metadata.get("data")
        if isinstance(data, str):
            return data
        return json.dumps(data, ensure_ascii=False, indent=2)
    raise ValueError(f"intelligence.extract cannot read kind {a.kind!r}")


def build_extract_messages(
    *, params: ExtractParams, schema: dict[str, Any], content: str
) -> tuple[str | None, str]:
    """(system, user) message pair shared by every extract backend."""
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
    user = (
        f"{params.prompt}\n\n"
        f"Return ONLY a JSON object that validates against this JSON schema. "
        f"No prose, no markdown fences:\n{schema_json}\n\n"
        f"CONTENT:\n{content}"
    )
    return params.system_prompt, user


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first top-level JSON object from a model reply."""
    cleaned = _FENCE_RE.sub("", text.strip())
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match is None:
        raise SchemaError("model reply contained no JSON object")
    try:
        obj: Any = json.loads(match.group())
    except json.JSONDecodeError as e:
        raise SchemaError(f"model reply was not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SchemaError(
            f"model reply JSON was {type(obj).__name__}, expected object"
        )
    return cast("dict[str, Any]", obj)


def build_extract_analysis(
    *,
    source: AnyArtifact,
    params: ExtractParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    raw_text: str,
    usage: dict[str, Any],
) -> Analysis:
    """Parse + schema-validate the model reply, then materialize an Analysis.

    Shared by every ``intelligence.extract`` backend so parsing and
    validation happen in exactly one place.
    """
    schema = load_schema(params.schema_def)
    data = parse_json_object(raw_text)
    validate(data, schema)  # raises SchemaError on mismatch

    derived_id = compute_derived_artifact_id(
        kind=Kind.Analysis,
        op_name="intelligence.extract",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[source.id],
    )
    payload: dict[str, Any] = {
        "data": data,
        "model": params.model,
        "usage": usage,
        "backend": backend_name,
    }
    tmp = workdir_path / f"extract-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return Analysis(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(source.id,),
        created_at=datetime.now(UTC),
    )


@register_op
class IntelligenceExtract(Operation):
    """Extract a profile-defined JSON object from a text artifact."""

    name = "intelligence.extract"
    version = "1.0.0"
    # One input, of any of these kinds. ``variadic_inputs`` makes the
    # engine validate membership instead of a fixed positional signature;
    # run() pins the arity to exactly one.
    input_kinds = _INPUT_KINDS
    variadic_inputs = True
    output_kinds = (Kind.Analysis,)
    params_model = ExtractParams
    default_backend = "gemini"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ExtractParams)
        if len(inputs) != 1 or inputs[0].kind not in _INPUT_KINDS:
            raise ValueError(
                f"intelligence.extract expects exactly one "
                f"Transcript|Markdown|Analysis input, "
                f"got {[a.kind for a in inputs]}"
            )
        # Fail fast on a bad schema before spending a model call.
        load_schema(params.schema_def)
        backend_name = _default_backend_for_model(params.model)
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute([inputs[0]], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, ExtractParams)
        backend_name = _default_backend_for_model(params.model)
        if backend_name == "mlx-lm":
            return CostEstimate(local_seconds=8.0)
        from media_engine.backends._pricing import estimate_cost_cents

        # Rough: content tokens unknown pre-read; price the output budget +
        # a nominal prompt. Real usage is recorded post-run.
        tok_in = 2000
        return CostEstimate(
            cloud_cents=estimate_cost_cents(
                params.model, tok_in, params.max_tokens
            ),
            tokens_in=tok_in,
            tokens_out=params.max_tokens,
        )


__all__ = [
    "ExtractParams",
    "IntelligenceExtract",
    "_default_backend_for_model",
    "artifact_to_text",
    "build_extract_analysis",
    "build_extract_messages",
    "parse_json_object",
]
