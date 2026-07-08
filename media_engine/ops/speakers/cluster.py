"""``speakers.cluster`` — SpeakerEmbedding… → SpeakerProfile… (fan-in).

Clusters per-turn voice fingerprints across one or more recordings (HDBSCAN),
assigning each cluster a **stable** ``Speaker_<sha8>`` id. Identity stays stable
across re-runs by reconciling each new cluster against already-persisted
profiles: a cluster whose centroid is within ``reconcile_threshold`` cosine of a
saved voice **reuses** that id (and updates the stored centroid as a running
mean); otherwise it mints a fresh id. See ``ops/speakers/_fingerprint.py``.

Persistence to the fingerprint DB (and therefore reconciliation) is gated on
``config.speaker_storage_enabled`` — voice fingerprints are biometric, so
storage is opt-in per namespace. With storage off the op still returns profiles
(minted statelessly from the centroid hash), it just writes nothing.

Backend: ``hdbscan`` (declares the clustering deps so ``med doctor`` surfaces
them; embedded ops hide their Python deps).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    SpeakerEmbedding,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)

OP_NAME = "speakers.cluster"
OP_VERSION = "1.0.0"


class ClusterParams(BaseModel):
    min_cluster_size: int = Field(default=2, ge=2)
    # HDBSCAN's min_samples; None → library default (== min_cluster_size).
    min_samples: int | None = Field(default=None, ge=1)
    # Skip turn vectors from turns shorter than this (matches embed_voice).
    min_turn_seconds: float = Field(default=0.5, ge=0.0)
    # Cosine ≥ this vs a saved profile → reuse its stable id (running-mean
    # update). Below → mint a new id.
    reconcile_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    # Persist resulting profiles to the fingerprint DB (still gated on
    # config.speaker_storage_enabled). Set False for a dry clustering.
    persist: bool = True


@register_op
class SpeakersCluster(Operation):
    """Cluster voice fingerprints into stable cross-recording identities."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = (Kind.SpeakerEmbedding,)
    variadic_inputs = True
    output_kinds = (Kind.SpeakerProfile,)
    params_model = ClusterParams
    default_backend = "hdbscan"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ClusterParams)
        if not inputs or not all(isinstance(a, SpeakerEmbedding) for a in inputs):
            raise ValueError(
                f"speakers.cluster expects one-or-more SpeakerEmbedding inputs, "
                f"got {[a.kind for a in inputs]}"
            )
        backend_name = ctx.backend or self.default_backend
        if backend_name is None:
            raise RuntimeError(
                f"{self.name} has no backend; register one or pass "
                f"`backend=` to Engine.run."
            )
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute(inputs, params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        total_turns = sum(
            len(a.turns) for a in inputs if isinstance(a, SpeakerEmbedding)
        )
        # HDBSCAN is ~O(n log n); tiny in practice. Fixed floor + per-turn.
        return CostEstimate(local_seconds=0.001 * total_turns + 0.5)


def gather_turn_vectors(
    inputs: list[SpeakerEmbedding], min_turn_seconds: float
) -> tuple[list[list[float]], list[str], int]:
    """Flatten all inputs' per-turn vectors (respecting the duration filter).

    Returns ``(vectors, member_ids, skipped)`` where ``member_ids[i]`` is the
    source SpeakerEmbedding id for ``vectors[i]`` and ``skipped`` counts turns
    dropped as too short.
    """
    vectors: list[list[float]] = []
    member_ids: list[str] = []
    skipped = 0
    for emb in inputs:
        for turn in emb.turns:
            vec = turn.get("vector")
            dur = float(turn.get("end", 0.0)) - float(turn.get("start", 0.0))
            if not vec:
                continue
            if dur < min_turn_seconds:
                skipped += 1
                continue
            vectors.append([float(x) for x in vec])
            member_ids.append(emb.id)
    return vectors, member_ids, skipped


def build_speaker_profile_payload(
    *,
    speaker_id: str,
    centroid: list[float],
    member_ids: list[str],
    member_count: int,
    model: str | None,
    reused: bool,
) -> dict[str, Any]:
    """The metadata payload for one SpeakerProfile artifact."""
    return {
        "speaker_id": speaker_id,
        "centroid": centroid,
        "member_ids": member_ids,
        "member_count": member_count,
        "model": model,
        "reused": reused,
    }


__all__ = [
    "OP_NAME",
    "OP_VERSION",
    "ClusterParams",
    "SpeakersCluster",
    "build_speaker_profile_payload",
    "gather_turn_vectors",
]
