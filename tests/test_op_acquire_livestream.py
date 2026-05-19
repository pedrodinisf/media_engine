"""Tests for ops/acquire/livestream.py + the ffmpeg-recorder backend.

Real-ffmpeg recording runs against a stdlib HTTP server serving the
finite synthetic ``tiny_hls/`` fixture (no network). The manual-split
branch is exercised deterministically by monkeypatching the per-segment
recorder, so it needs neither real timing nor a live source.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from media_engine.artifacts import Kind, Video
from media_engine.backends.acquire import ffmpeg_recorder as fr
from media_engine.ops import OperationContext
from media_engine.ops.acquire.livestream import (
    AcquireLivestream,
    AcquireLivestreamParams,
)
from media_engine.runtime.engine import Engine

FFMPEG = shutil.which("ffmpeg") is not None


# ─────────────────────────────────────────────────────────────────
# Op contract
# ─────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert AcquireLivestream.name == "acquire.livestream"
    assert AcquireLivestream.input_kinds == ()
    assert AcquireLivestream.output_kinds == (Kind.Video,)
    assert AcquireLivestream.default_backend == "ffmpeg-recorder"


def test_params_defaults() -> None:
    p = AcquireLivestreamParams(url="https://x/live")
    assert p.quality == "best"
    assert p.max_duration_sec is None
    assert p.segment_seconds is None
    assert "backend" not in AcquireLivestreamParams.model_fields


def test_cost_estimate_uses_max_duration() -> None:
    op = AcquireLivestream()
    bounded = op.cost_estimate([], AcquireLivestreamParams(url="x", max_duration_sec=42))
    assert bounded.local_seconds == 42.0
    unbounded = op.cost_estimate([], AcquireLivestreamParams(url="x"))
    assert unbounded.local_seconds > 0


def test_backend_registered() -> None:
    from media_engine.backends import BackendRegistry

    assert "ffmpeg-recorder" in BackendRegistry.for_op("acquire.livestream")


# ─────────────────────────────────────────────────────────────────
# LiveSegmentController + active-recorder registry
# ─────────────────────────────────────────────────────────────────


def test_controller_split_and_stop() -> None:
    c = fr.LiveSegmentController()
    assert not c.should_split()
    c.request_split()
    assert c.should_split() and c.segment_count == 1
    c.clear_split()
    assert not c.should_split()
    c.stop()
    assert c.stopped()
    c.request_split()  # stopped → no-op
    assert not c.should_split()


def test_request_split_all_signals_active() -> None:
    c1, c2 = fr.LiveSegmentController(), fr.LiveSegmentController()
    fr._register_active(c1)
    fr._register_active(c2)
    try:
        n = fr.request_split_all()
        assert n == 2
        assert c1.should_split() and c2.should_split()
    finally:
        fr._unregister_active(c1)
        fr._unregister_active(c2)
    assert fr.request_split_all() == 0


def test_to_pynput_hotkey() -> None:
    from media_engine.cli.acquire_live import _to_pynput_hotkey

    assert _to_pynput_hotkey("cmd+shift+j") == "<cmd>+<shift>+j"
    assert _to_pynput_hotkey("ctrl+k") == "<ctrl>+k"


# ─────────────────────────────────────────────────────────────────
# Local HLS server (finite synthetic stream)
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def hls_url(tiny_hls_dir: Path) -> Iterator[str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(tiny_hls_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/index.m3u8"
    finally:
        httpd.shutdown()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
async def test_record_finite_stream_single_segment(
    engine: Engine, hls_url: str
) -> None:
    [v] = await engine.run("acquire.livestream", url=hls_url)
    assert isinstance(v, Video)
    assert v.kind is Kind.Video
    assert v.path.exists()
    assert v.metadata["live"] is True
    assert v.metadata["segment_index"] == 0
    assert v.duration is not None and v.duration > 0


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
async def test_segment_seconds_yields_multiple(
    engine: Engine, hls_url: str
) -> None:
    out = await engine.run(
        "acquire.livestream", url=hls_url, segment_seconds=1
    )
    assert len(out) >= 2
    assert [a.metadata["segment_index"] for a in out] == list(range(len(out)))
    assert all(isinstance(a, Video) for a in out)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
async def test_cache_hit_on_rerun(
    engine: Engine, hls_url: str, mocker
) -> None:
    out1 = await engine.run("acquire.livestream", url=hls_url)
    spy = mocker.spy(fr.FfmpegRecorderBackend, "execute")
    out2 = await engine.run("acquire.livestream", url=hls_url)
    assert spy.call_count == 0  # served from cache
    assert [a.id for a in out1] == [a.id for a in out2]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
async def test_param_change_yields_new_ids(
    engine: Engine, hls_url: str
) -> None:
    [a] = await engine.run("acquire.livestream", url=hls_url)
    [b] = await engine.run("acquire.livestream", url=hls_url + "?v=2")
    assert a.id != b.id


async def test_rejects_inputs(engine: Engine, sample_mp4: Path) -> None:
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
        await engine.run("acquire.livestream", inputs=[v.id], url="https://x")


async def test_no_stream_found_raises(engine: Engine) -> None:
    # No ".m3u8" → playwright sniff path; playwright absent → RuntimeError.
    with pytest.raises(RuntimeError):
        await engine.run(
            "acquire.livestream", url="https://example.com/not-a-stream"
        )


# ─────────────────────────────────────────────────────────────────
# Manual-split branch (deterministic — fake per-segment recorder)
# ─────────────────────────────────────────────────────────────────


async def test_manual_split_starts_new_session(
    engine: Engine, tiny_hls_dir: Path, mocker
) -> None:
    calls = {"n": 0}
    seg_bytes = (tiny_hls_dir / "seg_000.ts").read_bytes()

    def fake_session(
        *, ffmpeg_path, stream_url, out_dir, segment_seconds, max_seconds, controller
    ):
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "seg.mp4"
        p.write_bytes(seg_bytes)
        calls["n"] += 1
        # Session 1 ends because the user requested a split; session 2
        # ends naturally with data → outer loop stops.
        return (0, True, [p]) if calls["n"] == 1 else (0, False, [p])

    mocker.patch.object(fr, "_resolve_stream_url", return_value="http://x/s.m3u8")
    mocker.patch.object(fr, "_run_ffmpeg_session", side_effect=fake_session)

    out = await engine.run("acquire.livestream", url="http://x/page")
    assert calls["n"] == 2
    assert [a.metadata["segment_index"] for a in out] == [0, 1]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_cli_acquire_live_smoke(
    tmp_path: Path, monkeypatch, tiny_hls_dir: Path
) -> None:
    from typer.testing import CliRunner

    from media_engine.cli import app

    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")

    handler = partial(SimpleHTTPRequestHandler, directory=str(tiny_hls_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        result = CliRunner().invoke(
            app,
            ["acquire-live", f"http://127.0.0.1:{port}/index.m3u8"],
        )
    finally:
        httpd.shutdown()
    assert result.exit_code == 0, result.output
    assert result.output.strip()  # at least one segment id printed
