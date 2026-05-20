"""``search.semantic`` — k-NN over the engine's Embedding artifacts.

The op takes **exactly one** ``Embedding`` artifact (the *query*
vector) and returns an ``Analysis`` whose ``metadata['results']`` is
the ranked hit list ``[{artifact_id, embedding_id, kind, score}, ...]``.

Why an Embedding input rather than a free-form string? It keeps the
op pure: no embedding-model lookup, no transient artifacts, no
optional ML deps in the op itself. The CLI / ``search.hybrid``
composite handle string-query embedding upstream, then dispatch here
with the resulting Embedding id.

``refresh_nonce`` is the documented escape hatch: re-running with
identical params is otherwise an engine cache hit. Bump the nonce (or
the op version) to force a fresh ranking when new artifacts have
landed in the index.
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

OP_NAME = "search.semantic"
OP_VERSION = "1.0.0"


class SearchSemanticParams(BaseModel):
    top_k: int = 10
    kind_filter: tuple[Kind, ...] | None = None
    refresh_nonce: str | None = None


@register_op
class SearchSemantic(Operation):
    """Rank artifacts by cosine similarity to the query Embedding."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = (Kind.Embedding,)
    variadic_inputs = True  # engine validates membership; op enforces arity
    output_kinds = (Kind.Analysis,)
    params_model = SearchSemanticParams
    default_backend = "sqlite"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SearchSemanticParams)
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
