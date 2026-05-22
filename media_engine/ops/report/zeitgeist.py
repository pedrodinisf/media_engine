"""``report.zeitgeist`` — list[SessionAnalysis] -> aggregate MarkdownArtifact.

Variadic input: ≥1 SessionAnalysis. Aggregates across all of them and
hands the precomputed counters to a Jinja2 template.

Template context:

    {
        "sessions":      list[SessionAnalysis],   # input artifacts verbatim
        "aggregate":     dict {                   # see _aggregate()
            "avg_polarity": float | None,
            "polarity_count": int,
            "top_topics":     list[(str, int)],
            "top_entities":   list[(str, int)],
            "top_claims":     list[(str, int)],
            "top_speakers":   list[(str, int)],
            "n_sessions":     int,
            "n_windows":      int,
        },
        "title":         str | None,
        **params.extra_context,
    }
"""

from __future__ import annotations

import hashlib
import statistics
from collections import Counter
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

OP_NAME = "report.zeitgeist"
OP_VERSION = "1.0.0"


class ZeitgeistReportParams(BaseModel):
    # See SessionReportParams.template — ``exclude=True`` so the cache
    # key tracks template content via ``template_sha``, not file path.
    template: Path = Field(..., exclude=True)
    title: str | None = None
    extra_context: dict[str, Any] = Field(default_factory=lambda: {})
    top_n_topics: int = 20
    top_n_entities: int = 30
    top_n_claims: int = 20
    top_n_speakers: int = 20
    template_sha: str = Field(
        default="",
        description=(
            "Auto-derived sha of the template file's bytes. Clients "
            "should not set this — the value is computed at validation "
            "time and any client-supplied value is overwritten."
        ),
        json_schema_extra={"readOnly": True},
    )

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


def _str_iter(value: Any) -> list[str]:
    """Coerce ``value`` to a list of non-empty stripped strings.

    Tolerates None, non-list types, and non-string elements — anything
    that isn't a usable string is dropped silently. We do this rather
    than raising because the SessionAnalysis ``data`` field is an
    untyped JSON blob whose shape depends on a user-supplied schema;
    being lenient keeps aggregate reports working when a single window
    misses or mis-types a field.
    """
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in cast("list[Any]", value):
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _aggregate(
    sessions: list[SessionAnalysis],
    params: ZeitgeistReportParams,
) -> dict[str, Any]:
    """Cross-session counters. Pure function — easy to unit-test."""
    polarities: list[float] = []
    topic_counter: Counter[str] = Counter()
    entity_counter: Counter[str] = Counter()
    claim_counter: Counter[str] = Counter()
    speaker_counter: Counter[str] = Counter()
    n_windows = 0

    for sa in sessions:
        data_raw: Any = sa.metadata.get("data", [])
        data: list[dict[str, Any]] = (
            list(cast("list[dict[str, Any]]", data_raw))
            if isinstance(data_raw, list)
            else []
        )
        n_windows += len(data)
        for win in data:
            analysis_raw: Any = win.get("analysis") or {}
            if not isinstance(analysis_raw, dict):
                continue
            analysis = cast("dict[str, Any]", analysis_raw)
            sentiment_raw: Any = analysis.get("sentiment")
            if isinstance(sentiment_raw, dict):
                sentiment = cast("dict[str, Any]", sentiment_raw)
                pol: Any = sentiment.get("polarity")
                if isinstance(pol, (int, float)):
                    polarities.append(float(pol))
            topic_counter.update(_str_iter(analysis.get("topics")))
            entity_counter.update(_str_iter(analysis.get("entities")))
            claim_counter.update(_str_iter(analysis.get("claims")))
            spk: Any = win.get("speaker")
            if isinstance(spk, str) and spk:
                speaker_counter[spk] += 1

    return {
        "avg_polarity": (
            statistics.fmean(polarities) if polarities else None
        ),
        "polarity_count": len(polarities),
        "top_topics": topic_counter.most_common(params.top_n_topics),
        "top_entities": entity_counter.most_common(params.top_n_entities),
        "top_claims": claim_counter.most_common(params.top_n_claims),
        "top_speakers": speaker_counter.most_common(params.top_n_speakers),
        "n_sessions": len(sessions),
        "n_windows": n_windows,
    }


@register_op
class ReportZeitgeist(Operation):
    """Render an aggregate across multiple SessionAnalysis artifacts."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = (Kind.SessionAnalysis,)
    variadic_inputs = True
    output_kinds = (Kind.MarkdownArtifact,)
    params_model = ZeitgeistReportParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ZeitgeistReportParams)
        if not inputs:
            raise ValueError(
                "report.zeitgeist requires at least one SessionAnalysis input"
            )
        sessions: list[SessionAnalysis] = []
        for art in inputs:
            if not isinstance(art, SessionAnalysis):
                raise ValueError(
                    f"report.zeitgeist accepts SessionAnalysis inputs only, "
                    f"got {art.kind}"
                )
            sessions.append(art)

        agg = _aggregate(sessions, params)
        context: dict[str, Any] = {
            "sessions": sessions,
            "aggregate": agg,
            "title": params.title,
        }
        context.update(params.extra_context)
        rendered = render_template(Path(params.template), context)

        input_ids = sorted(s.id for s in sessions)
        derived_id = compute_derived_artifact_id(
            kind=Kind.MarkdownArtifact,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=input_ids,
        )
        tmp = ctx.workdir / f"zeitgeist-{derived_id[:12]}.md"
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
                    "n_sessions": agg["n_sessions"],
                    "n_windows": agg["n_windows"],
                    "source_session_ids": input_ids,
                },
                derived_from=tuple(input_ids),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        # O(total windows across all sessions); cheap.
        return CostEstimate(local_seconds=0.5)


__all__ = [
    "OP_NAME",
    "OP_VERSION",
    "ReportZeitgeist",
    "ZeitgeistReportParams",
    "_aggregate",
]
