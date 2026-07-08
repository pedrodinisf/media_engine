"""``pgvector`` backend for ``speakers.match`` — cosine NN over the Postgres
fingerprint table.

Sibling of the ``sqlite`` match backend; reads the same ``StoredProfile`` rows
from ``_speaker_store_pg`` (the postgres table the ``hdbscan`` cluster backend
persists to when ``MEDIA_ENGINE_SPEAKER_DB_URL`` is set). Import-clean: the
psycopg/pgvector deps are lazy-imported inside the store helper.

Cache keys are backend-versioned, so ``--backend pgvector`` produces fresh
derived ids without colliding with cached ``sqlite`` results.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.backends import _speaker_store_pg as pg
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.speakers.match import (
    OP_NAME,
    OP_VERSION,
    MatchParams,
    query_vectors,
    rank_matches,
)

BACKEND_NAME = "pgvector"
BACKEND_VERSION = "1.0.0"


@register_backend
class PgvectorMatchBackend(Backend):
    op_name = OP_NAME
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(
        services=["postgres"],
        env=["MEDIA_ENGINE_SPEAKER_DB_URL"],
    )

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, MatchParams)
        q_vecs = query_vectors(inputs)
        query = inputs[0]

        conn = pg.connect(None)
        try:
            candidates = [
                (p.speaker_id, p.label, p.centroid)
                for p in pg.list_profiles(conn, ctx.namespace)
            ]
        finally:
            conn.close()

        results = rank_matches(
            q_vecs, candidates,
            top_k=params.top_k, min_similarity=params.min_similarity,
        )

        derived_id = compute_derived_artifact_id(
            kind=Kind.Analysis,
            op_name=OP_NAME,
            op_version=OP_VERSION,
            backend_name=BACKEND_NAME,
            backend_version=BACKEND_VERSION,
            params=params,
            input_ids=[query.id],
        )
        payload = {
            "mode": "speaker_match",
            "backend": BACKEND_NAME,
            "query_embedding_id": query.id,
            "results": results,
        }
        tmp = ctx.workdir / f"speaker-match-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)
        return [
            Analysis(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=(query.id,),
                produced_by=uuid4().hex,
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.2)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PgvectorMatchBackend"]
