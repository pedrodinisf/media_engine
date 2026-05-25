"""``intelligence.summarize`` — thin wrapper over ``intelligence.extract``.

Fixed schema ``{summary: str, key_points: list[str]}``. The engine stays
domain-free: this is just a convenience profile baked into an op so common
"summarize this" calls don't each re-declare the schema.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.intelligence._models import INTELLIGENCE_MODELS
from media_engine.ops.intelligence.extract import (
    ExtractParams,
    IntelligenceExtract,
)

_INPUT_KINDS = (Kind.Transcript, Kind.MarkdownArtifact, Kind.Analysis)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_points"],
    "additionalProperties": False,
}


class SummarizeParams(BaseModel):
    model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(INTELLIGENCE_MODELS)}),
    ] = "gemini-2.5-flash"
    focus: str | None = None
    system_prompt: str | None = None
    temperature: float = 0.2
    max_tokens: int = 2048
    # Optional override for which backend the delegate `intelligence.extract`
    # call dispatches to. Mirrors ``audio.transcribe_diarized``'s
    # ``transcribe_backend`` / ``diarize_backend`` pattern. Precedence
    # in run(): this param > ctx.backend (operator-level --backend on the
    # composite) > extract's own model-prefix router. B-007.
    extract_backend: str | None = None


def _prompt(focus: str | None) -> str:
    base = (
        "Summarize the content below. Produce a concise prose `summary` and "
        "a `key_points` list of the most important takeaways."
    )
    return f"{base} Focus on: {focus}." if focus else base


@register_op
class IntelligenceSummarize(Operation):
    """Summarize a text artifact into {summary, key_points}."""

    name = "intelligence.summarize"
    version = "1.0.0"
    input_kinds = _INPUT_KINDS
    variadic_inputs = True
    output_kinds = (Kind.Analysis,)
    params_model = SummarizeParams
    # Composite: no backend layer — delegates to intelligence.extract,
    # which bills the spend. records_cost=False avoids double-counting.
    records_cost = False
    delegates_to = ("intelligence.extract",)

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SummarizeParams)
        if len(inputs) != 1 or inputs[0].kind not in _INPUT_KINDS:
            raise ValueError(
                f"intelligence.summarize expects exactly one "
                f"Transcript|Markdown|Analysis input, "
                f"got {[a.kind for a in inputs]}"
            )
        if ctx.run_op is None:
            raise RuntimeError(
                "intelligence.summarize requires ctx.run_op (call via "
                "Engine.run, not Operation.run directly)."
            )
        # Forward an operator-supplied backend override into the delegate
        # call. Precedence: explicit composite param > engine-level
        # --backend (ctx.backend) > delegate's own model-prefix router.
        extract_backend = params.extract_backend or ctx.backend
        extract_kwargs: dict[str, Any] = {
            "inputs": [inputs[0].id],
            "prompt": _prompt(params.focus),
            "schema_def": _SCHEMA,
            "model": params.model,
            "system_prompt": params.system_prompt,
            "temperature": params.temperature,
            "max_tokens": params.max_tokens,
        }
        if extract_backend is not None:
            extract_kwargs["backend"] = extract_backend
        return await ctx.run_op("intelligence.extract", **extract_kwargs)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, SummarizeParams)
        return IntelligenceExtract().cost_estimate(
            inputs,
            ExtractParams(
                prompt=_prompt(params.focus),
                schema_def=_SCHEMA,
                model=params.model,
                max_tokens=params.max_tokens,
            ),
        )


__all__ = ["IntelligenceSummarize", "SummarizeParams"]
