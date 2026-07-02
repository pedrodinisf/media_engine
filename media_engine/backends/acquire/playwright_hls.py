"""``playwright-hls`` backend for ``acquire.url``.

A headless Chromium loads the page, image/font requests are aborted
for speed, every ``.m3u8`` response is captured, the best master
playlist is picked, then ffmpeg stream-copies it to a local ``.mp4``
(no re-encode). Use this for sites yt-dlp can't handle
(``--backend playwright-hls``).

Optional dep (``playwright``): the import is lazy + inside the call path
so this module is import-clean and registered even when playwright is
absent — the dependency is only needed at ``execute()`` time.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.url import AcquireURLParams, build_acquired_video
from media_engine.runtime.events import Progress

BACKEND_NAME = "playwright-hls"
BACKEND_VERSION = "1.0.0"

_PLAY_SELECTORS = (
    ".jw-icon-playback",
    ".vjs-big-play-button",
    '[class*="play-button"]',
    'button[aria-label*="play" i]',
    '[data-testid="play-button"]',
    "video",
)
_ASSET_RE = re.compile(r"\.(png|jpg|jpeg|gif|svg|woff|woff2|ttf)$")


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore  # noqa: I001,PGH003
    except ImportError as e:
        raise RuntimeError(
            "playwright is not installed. Install with: "
            "uv sync --extra acquire-url && playwright install chromium"
        ) from e
    return sync_playwright  # type: ignore[no-any-return]


def _select_best_stream(m3u8_urls: list[str]) -> str | None:
    """Prefer master/index playlists, avoid audio-only."""
    if not m3u8_urls:
        return None
    seen: set[str] = set()
    unique = [u for u in m3u8_urls if not (u in seen or seen.add(u))]
    for keyword in ("master", "index"):
        for u in unique:
            lo = u.lower()
            if keyword in lo and "audio" not in lo:
                return u
    for u in unique:
        if "audio" not in u.lower():
            return u
    return unique[0]


def sniff_m3u8(url: str, *, nav_timeout_ms: int, settle_ms: int) -> tuple[str | None, str | None]:
    """Headless-Chromium sniff. Returns (best_m3u8_url, page_title).

    Synchronous (Playwright sync API) — call via ``asyncio.to_thread``.
    Reused by the ``ffmpeg-recorder`` backend so a livestream page URL
    funnels through the same sniff logic.
    """
    sync_playwright = _import_playwright()
    m3u8_urls: list[str] = []
    title: str | None = None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
            )
            def _abort(route: Any) -> Any:
                return route.abort()

            def _on_response(r: Any) -> None:
                if ".m3u8" in r.url:
                    m3u8_urls.append(r.url)

            context.route(_ASSET_RE, _abort)
            page = context.new_page()
            page.on("response", _on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                with contextlib.suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=settle_ms)
            except Exception:
                pass
            with contextlib.suppress(Exception):
                title = page.title() or None
            for selector in _PLAY_SELECTORS:
                try:
                    if page.is_closed():
                        break
                    el = page.locator(selector).first
                    if el.is_visible(timeout=300):
                        el.click(timeout=500)
                        break
                except Exception:
                    continue
            if not page.is_closed():
                with contextlib.suppress(Exception):
                    page.wait_for_timeout(3000 if m3u8_urls else settle_ms)
        finally:
            with contextlib.suppress(Exception):
                browser.close()
    return _select_best_stream(m3u8_urls), title


def _stream_copy(*, ffmpeg_path: str, stream_url: str, out_path: Path) -> None:
    """ffmpeg HLS → mp4 with no re-encode."""
    cmd = [
        ffmpeg_path,
        "-nostdin", "-y",
        "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-multiple_requests", "1",
        "-http_seekable", "0",
        "-i", stream_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=900)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg stream-copy failed: {stderr or '(no stderr)'}"
        ) from e


def _emit(ctx: OperationContext, run_id: str, fraction: float, message: str) -> None:
    with contextlib.suppress(Exception):
        ctx.emit(
            Progress(
                event_id=uuid4().hex,
                job_id=ctx.job_id,
                op_run_id=ctx.op_run_id or run_id,
                timestamp=datetime.now(UTC),
                fraction=max(0.0, min(1.0, fraction)),
                message=message,
                phase="playwright-hls",
            )
        )


@register_backend
class PlaywrightHlsAcquireBackend(Backend):
    op_name = "acquire.url"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(binaries=["ffmpeg"])

    @classmethod
    def health(cls):  # type: ignore[override]
        import importlib.util

        if importlib.util.find_spec("playwright") is None:
            return "unavailable"
        return "ok" if shutil.which("ffmpeg") else "degraded"

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, AcquireURLParams)
        ffmpeg_path = ctx.config.ffmpeg_path
        if shutil.which(ffmpeg_path) is None:
            raise RuntimeError(
                f"ffmpeg binary not found: {ffmpeg_path!r}. "
                "Install via `brew install ffmpeg`."
            )

        run_id = uuid4().hex
        _emit(ctx, run_id, 0.05, "loading page")
        stream_url, title = await asyncio.to_thread(
            sniff_m3u8, params.url, nav_timeout_ms=30000, settle_ms=15000
        )
        if not stream_url:
            raise RuntimeError(
                f"No HLS stream found at {params.url!r} "
                "(login/geo-restricted, or not an HLS page)."
            )

        _emit(ctx, run_id, 0.4, "stream found, downloading")
        scratch = ctx.workdir / f"hls-{run_id}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            out_path = scratch / "dl.mp4"
            await asyncio.to_thread(
                _stream_copy,
                ffmpeg_path=ffmpeg_path,
                stream_url=stream_url,
                out_path=out_path,
            )
            _emit(ctx, run_id, 1.0, "downloaded")
            video = build_acquired_video(
                params=params,
                backend_name=self.name,
                backend_version=self.version,
                downloaded_path=out_path,
                ctx=ctx,
                source_url=params.url,
                title=title,
            )
            return [video]
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=45.0)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PlaywrightHlsAcquireBackend"]
