"""``report.session`` — SessionAnalysis -> MarkdownArtifact via Jinja2.

The template path is profile-supplied; the renderer is given a frozen
context:

    {
        "session":       SessionAnalysis,
        "segments":      list[dict],         # session.metadata["data"]
        "model":         str | None,
        "backend":       str | None,
        "speaker_names": dict[str, str|None] (from speakers.identify),
        "title":         str | None,         # from params.title
        **params.extra_context,
    }

``template_sha`` is auto-derived from the template file's bytes via a
``model_validator(mode="before")``, so editing the .j2 invalidates the
cache the same way ``prompt_path`` does for ``intelligence.analyze``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field, model_validator

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    MarkdownArtifact,
    SessionAnalysis,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)

from ._jinja import render_template

OP_NAME = "report.session"
OP_VERSION = "1.0.0"


class SessionReportParams(BaseModel):
    # ``template`` is ``exclude=True`` so the cache key tracks template
    # *content* (via ``template_sha`` below), not its filesystem path.
    # Same content via two paths hits the cache; same path with edited
    # content invalidates it.
    template: Path = Field(..., exclude=True)
    title: str | None = None
    extra_context: dict[str, Any] = Field(default_factory=lambda: {})
    # Auto-derived; participates in canonical params so editing the
    # template invalidates the cache.
    template_sha: str = ""

    @model_validator(mode="before")
    @classmethod
    def _hash_template(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = cast("dict[str, Any]", data)
        tp = d.get("template")
        if tp:
            pth = tp if isinstance(tp, Path) else Path(str(tp))
            if pth.exists():
                d["template_sha"] = hashlib.sha256(
                    pth.read_bytes()
                ).hexdigest()[:16]
            else:
                d["template_sha"] = "missing"
        return d


@register_op
class ReportSession(Operation):
    """Render a SessionAnalysis through a Jinja2 markdown template."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = (Kind.SessionAnalysis,)
    output_kinds = (Kind.MarkdownArtifact,)
    params_model = SessionReportParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SessionReportParams)
        if len(inputs) != 1 or not isinstance(inputs[0], SessionAnalysis):
            raise ValueError(
                f"report.session expects exactly one SessionAnalysis input, "
                f"got {[a.kind for a in inputs]}"
            )
        sa: SessionAnalysis = inputs[0]
        context: dict[str, Any] = {
            "session": sa,
            "segments": list(sa.metadata.get("data", [])),
            "model": sa.metadata.get("model"),
            "backend": sa.metadata.get("backend"),
            "speaker_names": sa.metadata.get("speaker_names", {}),
            "title": params.title,
        }
        context.update(params.extra_context)
        rendered = render_template(Path(params.template), context)

        derived_id = compute_derived_artifact_id(
            kind=Kind.MarkdownArtifact,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[sa.id],
        )
        tmp = ctx.workdir / f"report-{derived_id[:12]}.md"
        tmp.write_text(rendered, encoding="utf-8")
        dest = ctx.storage.store_file(tmp, derived_id, ".md")
        tmp.unlink(missing_ok=True)
        return [
            MarkdownArtifact(
                id=derived_id,
                path=dest,
                metadata={
                    "title": params.title,
                    "template": str(params.template),
                    "n_segments": len(context["segments"]),
                    "source_session_id": sa.id,
                },
                derived_from=(sa.id,),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.2)


__all__ = ["OP_NAME", "OP_VERSION", "ReportSession", "SessionReportParams"]
