"""``speakers.match`` — SpeakerEmbedding → Analysis (ranked voice matches).

Takes one query SpeakerEmbedding (the per-turn vectors of a recording) and
ranks the persisted SpeakerProfiles by cosine similarity — "whose saved voice
does this sound like?". Each query turn is scored against every stored centroid;
scores are aggregated per candidate (max over turns) so a recording matches a
voice if *any* of its turns is a strong hit.

Mirrors ``search.semantic``: pure op, dispatches to a vector-store backend
(``sqlite`` default, ``pgvector`` for the Postgres deployment). Returns an
Analysis whose ``metadata['results']`` is ``[{speaker_id, label, score}, ...]``.
An empty fingerprint DB yields an empty result list (not an error).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from media_engine.artifacts import AnyArtifact, Kind, SpeakerEmbedding
from media_engine.backends import BackendRegistry
from media_engine.backends._vec import cosine
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)

OP_NAME = "speakers.match"
OP_VERSION = "1.0.0"


def query_vectors(inputs: list[AnyArtifact]) -> list[list[float]]:
    """Extract all per-turn vectors from the (single) query SpeakerEmbedding."""
    if len(inputs) != 1 or not isinstance(inputs[0], SpeakerEmbedding):
        raise ValueError(
            f"speakers.match expects exactly one SpeakerEmbedding input, "
            f"got {[a.kind for a in inputs]}"
        )
    out: list[list[float]] = []
    for turn in inputs[0].turns:
        vec = turn.get("vector")
        if vec:
            out.append([float(x) for x in vec])
    return out


def rank_matches(
    query_vecs: list[list[float]],
    candidates: list[tuple[str, str | None, list[float]]],
    *,
    top_k: int,
    min_similarity: float,
) -> list[dict[str, object]]:
    """Rank ``(speaker_id, label, centroid)`` candidates by best-turn cosine.

    Each candidate is scored by the *max* cosine over all query turns (a
    recording matches a voice if any turn is a strong hit). Filtered by
    ``min_similarity``, sorted descending, truncated to ``top_k``.
    """
    scored: list[dict[str, object]] = []
    for speaker_id, label, centroid in candidates:
        best = max((cosine(q, centroid) for q in query_vecs), default=0.0)
        if best >= min_similarity:
            scored.append(
                {"speaker_id": speaker_id, "label": label, "score": float(best)}
            )
    scored.sort(key=lambda r: r["score"], reverse=True)  # type: ignore[arg-type,return-value]
    return scored[:top_k]


class MatchParams(BaseModel):
    top_k: int = Field(default=5, ge=1)
    min_similarity: float = Field(default=0.5, ge=-1.0, le=1.0)
    refresh_nonce: str | None = None


@register_op
class SpeakersMatch(Operation):
    """Rank saved SpeakerProfiles by cosine similarity to a query voice."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = (Kind.SpeakerEmbedding,)
    variadic_inputs = True  # engine validates membership; op enforces arity
    output_kinds = (Kind.Analysis,)
    params_model = MatchParams
    default_backend = "sqlite"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, MatchParams)
        backend_name = ctx.backend or self.default_backend
        if backend_name is None:
            raise RuntimeError(
                f"{self.name} has no backend; pass `backend=` to Engine.run."
            )
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute(inputs, params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.2)


__all__ = [
    "OP_NAME",
    "OP_VERSION",
    "MatchParams",
    "SpeakersMatch",
    "query_vectors",
    "rank_matches",
]
