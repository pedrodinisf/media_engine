"""``hdbscan`` backend for ``speakers.cluster``.

Runs HDBSCAN over L2-normalized voice vectors (euclidean distance on unit
vectors is monotonic with cosine, so this clusters by voice similarity), then
reconciles each cluster to a stable ``Speaker_<sha8>`` id and — when storage is
enabled — persists the running-mean centroid to the fingerprint DB.

Optional deps: ``uv sync --extra cluster`` (hdbscan + scikit-learn + numpy).
The backend module is import-clean; the deps are imported lazily at
``execute()`` time so registration never fails.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    SpeakerEmbedding,
    SpeakerProfile,
    compute_derived_artifact_id,
)
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.backends import _speaker_store as store
from media_engine.backends._vec import l2_normalize
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.speakers._fingerprint import (
    ExistingProfile,
    reconcile,
    running_mean,
)
from media_engine.ops.speakers.cluster import (
    OP_NAME,
    OP_VERSION,
    ClusterParams,
    build_speaker_profile_payload,
    gather_turn_vectors,
)

BACKEND_NAME = "hdbscan"
BACKEND_VERSION = "1.0.0"


def _import_deps() -> tuple[Any, Any]:
    try:
        hdbscan = importlib.import_module("hdbscan")
        np = importlib.import_module("numpy")
    except ImportError as e:
        raise RuntimeError(
            "hdbscan / numpy not installed. Install with: uv sync --extra cluster"
        ) from e
    return hdbscan, np


def cluster_labels(
    vectors: list[list[float]],
    *,
    min_cluster_size: int,
    min_samples: int | None,
) -> list[int]:
    """Run HDBSCAN → per-vector cluster label (``-1`` = noise)."""
    hdbscan, np = _import_deps()
    if len(vectors) < min_cluster_size:
        return [-1] * len(vectors)
    x = np.asarray(vectors, dtype="float64")
    # HDBSCAN raises if min_samples > n; clamp so a large user value degrades
    # gracefully instead of crashing the op.
    effective_min_samples = (
        min(min_samples, len(vectors)) if min_samples is not None else None
    )
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=effective_min_samples,
        metric="euclidean",
    )
    labels = clusterer.fit_predict(x)
    return [int(v) for v in labels]


def group_by_label(
    vectors: list[list[float]],
    labels: list[int],
    member_ids: list[str],
) -> dict[int, tuple[list[list[float]], list[str]]]:
    """Group member vectors + source ids by cluster label, dropping noise."""
    groups: dict[int, tuple[list[list[float]], list[str]]] = {}
    for vec, label, mid in zip(vectors, labels, member_ids, strict=True):
        if label < 0:
            continue
        vecs, ids = groups.setdefault(label, ([], []))
        vecs.append(vec)
        ids.append(mid)
    return groups


@register_backend
class HdbscanClusterBackend(Backend):
    op_name = OP_NAME
    name = BACKEND_NAME
    version = BACKEND_VERSION
    # Declare the packages the call path actually imports (hdbscan pulls in
    # scikit-learn transitively). These resolve by importable name so
    # ``med doctor`` reports the real status.
    requires = BackendRequirements(services=["hdbscan", "numpy"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ClusterParams)
        embeddings = [a for a in inputs if isinstance(a, SpeakerEmbedding)]
        model = embeddings[0].model if embeddings else None

        vectors, member_ids, skipped = gather_turn_vectors(
            embeddings, params.min_turn_seconds
        )
        if skipped:
            _log(ctx, f"skipped {skipped} turns shorter than "
                      f"{params.min_turn_seconds}s")
        norm_vectors = [l2_normalize(v) for v in vectors]

        labels = cluster_labels(
            norm_vectors,
            min_cluster_size=params.min_cluster_size,
            min_samples=params.min_samples,
        )
        groups = group_by_label(norm_vectors, labels, member_ids)
        noise = sum(1 for label in labels if label < 0)
        if noise:
            _log(ctx, f"{noise} turn vectors left unclustered (noise)")
        if not groups:
            _log(ctx, "no clusters formed — returning no profiles")
            return []

        # Deterministic cluster order for stable output ordering.
        ordered = [groups[k] for k in sorted(groups)]
        centroids = [l2_normalize(_mean(vecs)) for vecs, _ids in ordered]

        # Reconcile vs persisted profiles only when storage is enabled.
        storage_on = bool(getattr(ctx.config, "speaker_storage_enabled", False))
        existing: list[ExistingProfile] = []
        conn = None
        if storage_on:
            conn = store.connect(ctx.config.permanent_store)
            existing = [
                ExistingProfile(p.speaker_id, p.centroid, p.member_count)
                for p in store.list_profiles(conn, ctx.namespace)
            ]
        try:
            decisions = reconcile(centroids, existing, params.reconcile_threshold)

            outputs: list[AnyArtifact] = []
            for (vecs, ids), centroid, decision in zip(
                ordered, centroids, decisions, strict=True
            ):
                member_id_set = sorted(set(ids))
                if storage_on and params.persist and conn is not None:
                    _persist(
                        conn, decision, centroid, vecs, model,
                        ctx.namespace,
                    )
                outputs.append(
                    _build_profile_artifact(
                        ctx=ctx,
                        params=params,
                        speaker_id=decision.speaker_id,
                        centroid=centroid,
                        member_ids=member_id_set,
                        member_count=len(vecs),
                        model=model,
                        reused=decision.reused,
                        input_ids=member_id_set,
                    )
                )
            return outputs
        finally:
            if conn is not None:
                conn.close()

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        total = sum(len(a.turns) for a in inputs if isinstance(a, SpeakerEmbedding))
        return CostEstimate(local_seconds=0.001 * total + 0.5)


def _mean(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dims = len(vectors[0])
    acc = [0.0] * dims
    for v in vectors:
        for i in range(dims):
            acc[i] += v[i]
    n = len(vectors)
    return [x / n for x in acc]


def _persist(
    conn: Any,
    decision: Any,
    centroid: list[float],
    new_vectors: list[list[float]],
    model: str | None,
    namespace: str,
) -> None:
    """Insert a minted profile, or update a reused one via running mean.

    Writes the SQLite sidecar always; additionally mirrors to the Postgres
    fingerprint table when a Postgres speaker-store URL is configured, so the
    ``pgvector`` match backend has rows to read.
    """
    if decision.reused and decision.matched_existing is not None:
        prior = decision.matched_existing
        updated = running_mean(prior.centroid, prior.member_count, new_vectors)
        profile = store.StoredProfile(
            speaker_id=decision.speaker_id, namespace=namespace, model=model,
            centroid=updated, member_count=prior.member_count + len(new_vectors),
            label=None,
        )
    else:
        profile = store.StoredProfile(
            speaker_id=decision.speaker_id, namespace=namespace, model=model,
            centroid=centroid, member_count=len(new_vectors), label=None,
        )
    store.upsert_profile(conn, profile)

    import contextlib

    from media_engine.backends import _speaker_store_pg as pg

    if pg.is_configured():
        with contextlib.suppress(Exception):
            pg_conn = pg.connect(len(profile.centroid))
            try:
                pg.upsert_profile(pg_conn, profile)
            finally:
                pg_conn.close()


def _build_profile_artifact(
    *,
    ctx: OperationContext,
    params: ClusterParams,
    speaker_id: str,
    centroid: list[float],
    member_ids: list[str],
    member_count: int,
    model: str | None,
    reused: bool,
    input_ids: list[str],
) -> SpeakerProfile:
    # speaker_id discriminates the per-cluster output; the snapshot centroid is
    # deterministic from the member vectors, so re-running on the same inputs
    # (storage off) yields the same artifact id — content-addressing holds.
    derived_id = compute_derived_artifact_id(
        kind=Kind.SpeakerProfile,
        op_name=OP_NAME,
        op_version=OP_VERSION,
        backend_name=BACKEND_NAME,
        backend_version=BACKEND_VERSION,
        params=params,
        input_ids=[*input_ids, speaker_id],
    )
    payload = build_speaker_profile_payload(
        speaker_id=speaker_id, centroid=centroid, member_ids=member_ids,
        member_count=member_count, model=model, reused=reused,
    )
    tmp = ctx.workdir / f"speaker-profile-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    dest = ctx.storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return SpeakerProfile(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=tuple(member_ids),
        created_at=datetime.now(UTC),
    )


def _log(ctx: OperationContext, message: str) -> None:
    import contextlib
    from uuid import uuid4

    from media_engine.runtime.events import LogLine

    with contextlib.suppress(Exception):
        ctx.emit(
            LogLine(
                event_id=uuid4().hex,
                op_run_id=ctx.op_run_id or "",
                job_id=ctx.job_id,
                timestamp=datetime.now(UTC),
                level="info",
                source="hdbscan",
                line=message,
            )
        )


__all__ = [
    "BACKEND_NAME",
    "BACKEND_VERSION",
    "HdbscanClusterBackend",
    "cluster_labels",
    "group_by_label",
]
