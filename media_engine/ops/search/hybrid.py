"""``search.hybrid`` — reciprocal-rank fusion of semantic + fulltext.

A thin composite: dispatch ``search.semantic`` (with the query
Embedding input) and ``search.fulltext`` (with the query string),
then fuse the two ranked lists by RRF::

    score(d) = Σ  1 / (rrf_k + rank_i(d))

over both modalities (``rrf_k=60`` is the canonical default from the
literature). Results are deduped by ``artifact_id`` so a hit found by
both ranks better than either alone — which is the whole point.

``records_cost = False`` so the wrapper doesn't double-bill the cost
of the two sub-ops it delegates to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)

OP_NAME = "search.hybrid"
OP_VERSION = "1.0.0"


class SearchHybridParams(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=1000)
    kind_filter: tuple[Kind, ...] | None = None
    rrf_k: int = Field(default=60, ge=1)
    refresh_nonce: str | None = None


def reciprocal_rank_fusion(
    rankings: list[list[dict[str, Any]]], *, rrf_k: int = 60
) -> list[dict[str, Any]]:
    """Fuse N ranked result lists by reciprocal rank.

    Each ``rankings[i]`` is the descending list of ``{artifact_id, ...}``
    dicts from one search modality. Returns a fused descending list
    where each entry carries the summed RRF score plus the original
    per-modality ranks for transparency.
    """
    fused: dict[str, dict[str, Any]] = {}
    for source_index, ranked in enumerate(rankings):
        for rank, hit in enumerate(ranked, start=1):
            art_id = hit.get("artifact_id")
            if not isinstance(art_id, str):
                continue
            entry = fused.setdefault(
                art_id,
                {
                    "artifact_id": art_id,
                    "kind": hit.get("kind"),
                    "score": 0.0,
                    "ranks": {},
                },
            )
            entry["score"] = float(entry["score"]) + 1.0 / (rrf_k + rank)
            entry["ranks"][str(source_index)] = rank
            # Carry the first non-empty snippet through.
            snip = hit.get("snippet")
            if snip and "snippet" not in entry:
                entry["snippet"] = snip
    out = list(fused.values())
    out.sort(key=lambda r: float(r["score"]), reverse=True)
    return out


@register_op
class SearchHybrid(Operation):
    """RRF-fuse a semantic query Embedding and a keyword query string."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = (Kind.Embedding,)
    variadic_inputs = True
    output_kinds = (Kind.Analysis,)
    params_model = SearchHybridParams
    records_cost = False  # composite — sub-ops bill their own spend
    delegates_to = ("search.semantic", "search.fulltext")

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SearchHybridParams)
        if len(inputs) != 1:
            raise ValueError(
                f"search.hybrid expects exactly one Embedding input, "
                f"got {[a.kind for a in inputs]}"
            )
        if ctx.run_op is None:
            raise RuntimeError(
                "search.hybrid is a composite — call it through Engine.run"
            )
        query_embedding = inputs[0]

        kind_filter = (
            list(params.kind_filter) if params.kind_filter else None
        )
        # We over-fetch per modality so the RRF top-k has signal to chew on.
        per_modality_k = max(params.top_k * 3, params.top_k)

        sem_outputs = await ctx.run_op(
            "search.semantic",
            inputs=[query_embedding.id],
            top_k=per_modality_k,
            kind_filter=kind_filter,
            refresh_nonce=params.refresh_nonce,
        )
        ft_outputs = await ctx.run_op(
            "search.fulltext",
            query=params.query,
            top_k=per_modality_k,
            kind_filter=kind_filter,
            refresh_nonce=params.refresh_nonce,
        )

        sem_results = list(sem_outputs[0].metadata.get("results", []))
        ft_results = list(ft_outputs[0].metadata.get("results", []))
        fused = reciprocal_rank_fusion(
            [sem_results, ft_results], rrf_k=params.rrf_k
        )[: params.top_k]

        derived_id = compute_derived_artifact_id(
            kind=Kind.Analysis,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[query_embedding.id],
        )
        payload = {
            "mode": "hybrid",
            "query": params.query,
            "query_embedding_id": query_embedding.id,
            "rrf_k": params.rrf_k,
            "results": fused,
            "components": {
                "semantic_id": sem_outputs[0].id,
                "fulltext_id": ft_outputs[0].id,
            },
        }
        tmp = ctx.workdir / f"search-hybrid-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)
        return [
            Analysis(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=(query_embedding.id, sem_outputs[0].id, ft_outputs[0].id),
                produced_by=uuid4().hex,
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.4)
