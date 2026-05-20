"""``search.fulltext`` — BM25 keyword search over text-bearing artifacts.

Returns an ``Analysis`` whose ``metadata['results']`` is the ranked
list ``[{artifact_id, kind, score, snippet}, ...]``. Indexes
``Transcript``, ``MarkdownArtifact``, ``Document``, ``WebPage``, and
``Chunks`` — every text-shaped kind the engine currently produces.

Default backend is ``sqlite-fts5`` (SQLite's built-in FTS5 virtual
table — no extra dep). Cache + ``refresh_nonce`` semantics match
``search.semantic``.
"""

from __future__ import annotations

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)

OP_NAME = "search.fulltext"
OP_VERSION = "1.0.0"


class SearchFulltextParams(BaseModel):
    query: str
    top_k: int = 10
    kind_filter: tuple[Kind, ...] | None = None
    refresh_nonce: str | None = None


@register_op
class SearchFulltext(Operation):
    """Rank text-bearing artifacts by BM25 against a keyword query."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = ()
    output_kinds = (Kind.Analysis,)
    params_model = SearchFulltextParams
    default_backend = "sqlite-fts5"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SearchFulltextParams)
        if inputs:
            raise ValueError(
                f"search.fulltext takes no inputs, "
                f"got {[a.kind for a in inputs]}"
            )
        backend_name = ctx.backend or self.default_backend
        if backend_name is None:
            raise RuntimeError(
                f"{self.name} has no backend; pass `backend=` to Engine.run."
            )
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute([], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.2)
