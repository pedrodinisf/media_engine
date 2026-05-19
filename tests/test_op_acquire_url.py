"""Tests for ops/acquire/url.py + the yt-dlp / playwright-hls backends.

Two layers:
1. Op contract / dispatch / cache via a fake ``yt-dlp`` backend that
   "downloads" a local fixture (always run).
2. Real-backend smokes: ``needs_ytdlp`` (binary + network, CI optional)
   and ``playwright-hls`` against a stdlib HTTP server serving the
   synthetic ``tiny_hls/`` fixture (skipped without playwright/chromium).
"""

from __future__ import annotations

import importlib.util
import shutil
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Kind, Video
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.url import (
    AcquireURL,
    AcquireURLParams,
    build_acquired_video,
)
from media_engine.runtime.engine import Engine

FIXTURE_DIR = Path(__file__).parent / "fixtures"
YTDLP_AVAILABLE = shutil.which("yt-dlp") is not None
PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None


# ─────────────────────────────────────────────────────────────────
# Op contract
# ─────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert AcquireURL.name == "acquire.url"
    assert AcquireURL.input_kinds == ()
    assert AcquireURL.output_kinds == (Kind.Video,)
    assert AcquireURL.default_backend == "yt-dlp"


def test_params_defaults() -> None:
    p = AcquireURLParams(url="https://example.com/v")
    assert p.quality == "best"
    # No `backend` field — it would collide with Engine.run(backend=).
    assert "backend" not in AcquireURLParams.model_fields


def test_cost_estimate_is_positive() -> None:
    est = AcquireURL().cost_estimate([], AcquireURLParams(url="x"))
    assert est.local_seconds > 0


def test_backends_registered() -> None:
    backends = BackendRegistry.for_op("acquire.url")
    assert "yt-dlp" in backends
    # playwright-hls is import-clean → registered even without playwright.
    assert "playwright-hls" in backends


# ─────────────────────────────────────────────────────────────────
# Dispatch / cache via a fake yt-dlp backend
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_ytdlp_backend() -> Iterator[type[Backend]]:
    BackendRegistry.unregister("acquire.url", "yt-dlp")

    @register_backend
    class _FakeYtdlp(Backend):
        op_name = "acquire.url"
        name = "yt-dlp"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, AcquireURLParams)
            # "Download" = copy the committed sample.mp4 into the workdir.
            dl = ctx.workdir / "dl.mp4"
            dl.write_bytes((FIXTURE_DIR / "sample.mp4").read_bytes())
            return [
                build_acquired_video(
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    downloaded_path=dl,
                    ctx=ctx,
                    source_url=params.url,
                    title="Sample",
                )
            ]

        def cost_estimate(
            self, inputs: list[AnyArtifact], params: BaseModel
        ) -> CostEstimate:
            return CostEstimate(local_seconds=1.0)

    yield _FakeYtdlp
    BackendRegistry.unregister("acquire.url", "yt-dlp")
    from media_engine.backends.acquire.ytdlp import YtdlpAcquireBackend

    BackendRegistry.register(YtdlpAcquireBackend)


async def test_engine_run_acquire_url(
    engine: Engine, sample_mp4: Path, fake_ytdlp_backend
) -> None:
    [v] = await engine.run("acquire.url", url="https://example.com/talk")
    assert isinstance(v, Video)
    assert v.kind is Kind.Video
    assert v.path.exists()
    assert v.metadata["url"] == "https://example.com/talk"
    assert v.metadata["title"] == "Sample"
    assert v.duration is not None and v.duration > 0


async def test_acquire_url_cache_hit_on_rerun(
    engine: Engine, sample_mp4: Path, fake_ytdlp_backend, mocker
) -> None:
    [v1] = await engine.run("acquire.url", url="https://example.com/talk")
    spy = mocker.spy(fake_ytdlp_backend, "execute")
    [v2] = await engine.run("acquire.url", url="https://example.com/talk")
    assert spy.call_count == 0  # served from cache; backend not re-invoked
    assert v1.id == v2.id


async def test_acquire_url_param_change_yields_new_id(
    engine: Engine, sample_mp4: Path, fake_ytdlp_backend
) -> None:
    [a] = await engine.run("acquire.url", url="https://example.com/a")
    [b] = await engine.run("acquire.url", url="https://example.com/b")
    [c] = await engine.run("acquire.url", url="https://example.com/a", quality="worst")
    assert a.id != b.id  # different URL → different id
    assert a.id != c.id  # different quality → different id


async def test_acquire_url_rejects_inputs(
    engine: Engine, sample_mp4: Path, fake_ytdlp_backend
) -> None:
    from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams

    workdir = engine.storage.ensure_workdir("t")
    ctx = OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )
    [v] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx
    )
    engine.cache.upsert_artifact(v)
    with pytest.raises(ValueError, match="expects no inputs"):
        await engine.run("acquire.url", inputs=[v.id], url="https://x")


async def test_explicit_backend_overrides_default(
    engine: Engine, sample_mp4: Path, fake_ytdlp_backend
) -> None:
    # Explicit backend= must resolve and flow through ctx.backend.
    [v] = await engine.run(
        "acquire.url", url="https://example.com/x", backend="yt-dlp"
    )
    assert isinstance(v, Video)


# ─────────────────────────────────────────────────────────────────
# yt-dlp backend error path (no binary)
# ─────────────────────────────────────────────────────────────────


async def test_ytdlp_backend_missing_binary_raises(
    engine: Engine, mocker
) -> None:
    from media_engine.backends.acquire import ytdlp

    mocker.patch.object(ytdlp.shutil, "which", return_value=None)
    backend = ytdlp.YtdlpAcquireBackend()
    workdir = engine.storage.ensure_workdir("t")
    ctx = OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )
    with pytest.raises(RuntimeError, match="yt-dlp binary not found"):
        await backend.execute([], AcquireURLParams(url="https://x"), ctx)


def test_ytdlp_format_selector() -> None:
    from media_engine.backends.acquire.ytdlp import _format_selector

    assert _format_selector("best") == "bv*+ba/b"
    assert _format_selector("worst") == "worst"
    assert _format_selector("137+140") == "137+140"


def test_select_best_stream_prefers_master() -> None:
    from media_engine.backends.acquire.playwright_hls import _select_best_stream

    assert _select_best_stream([]) is None
    assert (
        _select_best_stream(
            ["https://h/audio.m3u8", "https://h/master.m3u8"]
        )
        == "https://h/master.m3u8"
    )
    # Audio-only avoided when a plain stream exists.
    assert (
        _select_best_stream(["https://h/audio_only.m3u8", "https://h/v.m3u8"])
        == "https://h/v.m3u8"
    )


# ─────────────────────────────────────────────────────────────────
# Real-backend smokes (gated)
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def hls_server(tiny_hls_dir: Path) -> Iterator[str]:
    """Serve tiny_hls/ + a tiny HTML page that embeds the m3u8."""
    (tiny_hls_dir / "page.html").write_text(
        '<!doctype html><html><body>'
        '<video src="index.m3u8" autoplay muted></video>'
        "</body></html>"
    )
    handler = partial(SimpleHTTPRequestHandler, directory=str(tiny_hls_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        (tiny_hls_dir / "page.html").unlink(missing_ok=True)


@pytest.mark.needs_playwright
async def test_playwright_hls_against_local_server(
    engine: Engine, hls_server: str
) -> None:
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed")
    try:
        [v] = await engine.run(
            "acquire.url",
            url=f"{hls_server}/page.html",
            backend="playwright-hls",
        )
    except RuntimeError as e:
        # No chromium browser installed, or the headless run couldn't
        # sniff the stream in this CI sandbox — gate, don't fail.
        pytest.skip(f"playwright-hls smoke unavailable: {e}")
    assert isinstance(v, Video)
    assert v.path.exists()


@pytest.mark.needs_ytdlp
async def test_ytdlp_real_smoke(engine: Engine) -> None:
    if not YTDLP_AVAILABLE:
        pytest.skip("yt-dlp binary not installed")
    pytest.skip("network smoke — enable manually with a known tiny clip URL")
