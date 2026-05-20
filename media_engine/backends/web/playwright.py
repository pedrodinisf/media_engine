"""``playwright`` backend for ``web.fetch`` — JS-rendered DOM read.

For SPAs / heavy-JS sites the static httpx fetch would only see the
shell. This backend opens headless Chromium, navigates, waits for
network idle, and reads ``page.content()`` after JS has run. Lazy
playwright import (import-clean module; registered in bootstrap's
try/except optional-dep block).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

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

BACKEND_NAME = "playwright"
BACKEND_VERSION = "1.0.0"


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore  # noqa: I001,PGH003
    except ImportError as e:
        raise RuntimeError(
            "playwright is not installed. Install with: "
            "uv sync --extra acquire-url && playwright install chromium"
        ) from e
    return sync_playwright  # type: ignore[no-any-return]


def _fetch_sync(
    url: str, *, nav_timeout_ms: int = 30000, settle_ms: int = 15000
) -> tuple[int, str | None, str]:
    sync_playwright = _import_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            page = browser.new_page()
            resp = page.goto(
                url, wait_until="domcontentloaded", timeout=nav_timeout_ms
            )
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=settle_ms)
            status = int(resp.status) if resp is not None else 0
            ctype = resp.headers.get("content-type") if resp is not None else None
            html = page.content()
            return status, ctype, html
        finally:
            with contextlib.suppress(Exception):
                browser.close()


@register_backend
class PlaywrightWebFetchBackend(Backend):
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
        return CostEstimate(local_seconds=15.0)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PlaywrightWebFetchBackend"]
