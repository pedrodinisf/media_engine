"""``httpx`` backend for ``web.fetch`` — static GET, no browser."""

from __future__ import annotations

import asyncio

import httpx
from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.web.fetch import WebFetchParams

from ._html import build_webpage_artifact

BACKEND_NAME = "httpx"
BACKEND_VERSION = "1.0.0"

_USER_AGENT = "media-engine/0.1 (+https://github.com/pedrodinis/media_engine)"


def _fetch_sync(url: str) -> tuple[int, str | None, str]:
    """Plain GET. Returns (status_code, content_type, decoded body)."""
    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html, */*"},
    ) as client:
        resp = client.get(url)
    ct = resp.headers.get("content-type")
    return resp.status_code, ct, resp.text


@register_backend
class HttpxWebFetchBackend(Backend):
    op_name = "web.fetch"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements()

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, WebFetchParams)
        # httpx is a core dep but the call blocks on the network — keep
        # the event loop responsive by running it in a worker thread.
        status, ctype, html = await asyncio.to_thread(_fetch_sync, params.url)
        return [
            build_webpage_artifact(
                params=params,
                backend_name=self.name,
                backend_version=self.version,
                html=html,
                status_code=status,
                content_type=ctype,
                ctx=ctx,
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=1.0)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "HttpxWebFetchBackend"]
