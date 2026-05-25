"""``intelligence.classify`` — thin wrapper over ``intelligence.extract``.

Fixed schema ``{labels: list[str], confidence: {label: float}, rationale:
str}``. Caller supplies the candidate taxonomy (engine has no opinion on
what the labels mean).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

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
        "labels": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "object"},
        "rationale": {"type": "string"},
    },
    "required": ["labels", "confidence", "rationale"],
    "additionalProperties": False,
}


class ClassifyParams(BaseModel):
    labels: list[str]
    multi_label: bool = False
    model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(INTELLIGENCE_MODELS)}),
    ] = "gemini-2.5-flash"
    system_prompt: str | None = None
    temperature: float = 0.0
    max_tokens: int = 1024

    @field_validator("labels")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "intelligence.classify requires at least one candidate label"
            )
        return v


def _prompt(labels: list[str], multi_label: bool) -> str:
    taxonomy = ", ".join(labels)
    pick = (
        "Select every label that applies"
        if multi_label
        else "Select the single best label"
    )
    return (
        f"Classify the content below against this taxonomy: {taxonomy}.\n"
        f"{pick}. Put the chosen label(s) in `labels`, a per-label "
        f"probability 0..1 map in `confidence`, and a one-sentence "
        f"`rationale`."
    )


@register_op
class IntelligenceClassify(Operation):
    """Classify a text artifact against a caller-supplied taxonomy."""

    name = "intelligence.classify"
    version = "1.0.0"
    input_kinds = _INPUT_KINDS
    variadic_inputs = True
    output_kinds = (Kind.Analysis,)
    params_model = ClassifyParams
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
        assert isinstance(params, ClassifyParams)
        if len(inputs) != 1 or inputs[0].kind not in _INPUT_KINDS:
            raise ValueError(
                f"intelligence.classify expects exactly one "
                f"Transcript|Markdown|Analysis input, "
                f"got {[a.kind for a in inputs]}"
            )
        if ctx.run_op is None:
            raise RuntimeError(
                "intelligence.classify requires ctx.run_op (call via "
                "Engine.run, not Operation.run directly)."
            )
        return await ctx.run_op(
            "intelligence.extract",
            inputs=[inputs[0].id],
            prompt=_prompt(params.labels, params.multi_label),
            schema_def=_SCHEMA,
            model=params.model,
            system_prompt=params.system_prompt,
            temperature=params.temperature,
            max_tokens=params.max_tokens,
        )

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, ClassifyParams)
        return IntelligenceExtract().cost_estimate(
            inputs,
            ExtractParams(
                prompt=_prompt(params.labels, params.multi_label),
                schema_def=_SCHEMA,
                model=params.model,
                max_tokens=params.max_tokens,
            ),
        )


# Public aliases — reused by intelligence.analyze's optional classify pass.
CLASSIFY_SCHEMA = _SCHEMA
classify_prompt = _prompt

__all__ = [
    "CLASSIFY_SCHEMA",
    "ClassifyParams",
    "IntelligenceClassify",
    "classify_prompt",
]
