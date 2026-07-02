"""Regression — backends' Progress events carry ``job_id``.

Phase 6.7's b8a3be9 fix added ``job_id=ctx.job_id`` to the load-bearing
Progress emitters (mlx_whisper, pyannote, vllm_mlx, extract_audio) but
missed five sibling sites — three in ``backends/acquire/`` plus
``backends/video_multimodal/gemini.py`` and ``ops/metadata/scrape_page``.
Without ``job_id``, the API's SSE per-job filter at ``api/sse.py:67``
silently drops the events; the Web UI Job-detail Events tab stays empty
during long acquires / cloud-VLM uploads / page scrapes.

Audit findings F-003 (playwright_hls), F-004 (ffmpeg_recorder),
F-005 (ytdlp), F-006 (gemini), F-007 (scrape_page). The bug is
identical across all five — Progress(...) constructor missing
``job_id=ctx.job_id``.

Test invokes each helper directly with a fake ``OperationContext`` and
asserts the captured Progress event carries ``ctx.job_id``.
"""
from __future__ import annotations

from media_engine.backends.acquire.ffmpeg_recorder import _emit as ffmpeg_emit
from media_engine.backends.acquire.playwright_hls import _emit as playwright_emit
from media_engine.backends.acquire.ytdlp import _emit as ytdlp_emit
from media_engine.backends.video_multimodal.gemini import _emit as gemini_vmm_emit
from media_engine.ops._base import OperationContext
from media_engine.runtime.engine import Engine
from media_engine.runtime.events import Event, Progress


def _ctx_with_job(engine: Engine) -> tuple[OperationContext, list[Event]]:
    captured: list[Event] = []
    workdir = engine.storage.ensure_workdir("test-acquire-job-id")
    ctx = OperationContext(
        workdir=workdir,
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=captured.append,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
        job_id="job-42",
        op_run_id="run-1",
    )
    return ctx, captured


def test_ytdlp_emit_carries_job_id(engine: Engine) -> None:
    ctx, captured = _ctx_with_job(engine)
    ytdlp_emit(ctx, "local-run-id", 0.5, "downloading")
    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, Progress)
    assert ev.job_id == "job-42"


def test_playwright_hls_emit_carries_job_id(engine: Engine) -> None:
    ctx, captured = _ctx_with_job(engine)
    playwright_emit(ctx, "local-run-id", 0.5, "sniffing")
    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, Progress)
    assert ev.job_id == "job-42"


def test_ffmpeg_recorder_emit_carries_job_id(engine: Engine) -> None:
    ctx, captured = _ctx_with_job(engine)
    ffmpeg_emit(ctx, "local-run-id", 0.5, "recording")
    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, Progress)
    assert ev.job_id == "job-42"


def test_gemini_video_multimodal_emit_carries_job_id(engine: Engine) -> None:
    ctx, captured = _ctx_with_job(engine)
    gemini_vmm_emit(ctx, "local-run-id", 0.5, "uploading")
    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, Progress)
    assert ev.job_id == "job-42"


def test_scrape_page_progress_carries_job_id(engine: Engine, monkeypatch) -> None:
    """metadata.scrape_page emits a single Progress at the start of run().

    Patch the playwright scrape helper out so the test stays offline and
    only the Progress emit + downstream cache write fire.
    """
    import asyncio

    import media_engine.ops.metadata.scrape_page as sp

    captured: list[Event] = []
    workdir = engine.storage.ensure_workdir("test-scrape-job-id")
    ctx = OperationContext(
        workdir=workdir,
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=captured.append,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
        job_id="job-77",
        op_run_id="run-2",
    )
    monkeypatch.setattr(
        sp, "_scrape",
        lambda url: {"url": url, "title": "stub", "text": "stub"}
    )
    op = sp.MetadataScrapePage()
    params = sp.ScrapePageParams(url="https://example.invalid/")
    asyncio.run(op.run([], params, ctx))
    progresses = [e for e in captured if isinstance(e, Progress)]
    assert len(progresses) >= 1
    assert progresses[0].job_id == "job-77"
