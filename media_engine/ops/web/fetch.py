"""``web.fetch`` — pull a URL into a typed ``WebPage`` artifact.

Two backends. ``httpx`` (default) is a plain static GET — fast, no
browser; works for any server that returns HTML without client-side
rendering. ``playwright`` opens headless Chromium and reads the
post-render DOM — pick it for SPAs / heavy-JS pages
(``render_js=True``). Both produce the same shape so downstream ops
don't branch.

Identity = derived id over ``{url, render_js}`` — consistent with
``acquire.url`` (re-fetching the same URL with the same render mode
is an engine cache hit; tweak a param or bump ``op.version`` to force
a re-fetch).
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

OP_NAME = "web.fetch"
OP_VERSION = "1.0.0"


class WebFetchParams(BaseModel):
    url: str
    render_js: bool = False


@register_op
class WebFetch(Operation):
    """Fetch a URL into a typed WebPage (title + plaintext + status)."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = ()
    output_kinds = (Kind.WebPage,)
    params_model = WebFetchParams
    default_backend = "httpx"

    def select_backend(self, params: BaseModel) -> str | None:
        assert isinstance(params, WebFetchParams)
        return "playwright" if params.render_js else None

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, WebFetchParams)
        if inputs:
            raise ValueError(
                f"web.fetch takes no inputs, got {[a.kind for a in inputs]}"
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
        assert isinstance(params, WebFetchParams)
        # Static GET is cheap; headless browser nav dominates by ~30×.
        return CostEstimate(local_seconds=15.0 if params.render_js else 1.0)
