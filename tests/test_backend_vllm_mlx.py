"""Tests for the vllm-mlx video.multimodal backend.

The real path needs the vllm-mlx binary + a model + RAM, so it's gated.
What we *can* test hermetically: binary discovery, message construction
(frames → OpenAI content), timestamp reconstruction, and a fully-mocked
end-to-end (fake ServerManager + httpx) so the orchestration is covered.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_engine.artifacts import FrameSet
from media_engine.backends.video_multimodal.vllm_mlx import (
    VllmMlxVideoMultimodalBackend,
    _build_messages,
    _frame_timestamp,
    find_vllm_mlx_binary,
)
from media_engine.ops import OperationContext
from media_engine.ops.video.multimodal import MultimodalVideoParams
from media_engine.runtime.engine import Engine


def _vllm_real_runnable() -> bool:
    """Real smoke needs the binary AND enough RAM for a 7B model."""
    if shutil.which("vllm-mlx") is None:
        return False
    from media_engine.runtime.hardware import check_model_fits

    return check_model_fits(8.0, headroom_gb=4.0).fits


VLLM_AVAILABLE = _vllm_real_runnable()


def test_backend_attributes() -> None:
    assert VllmMlxVideoMultimodalBackend.op_name == "video.multimodal"
    assert VllmMlxVideoMultimodalBackend.name == "vllm-mlx"
    assert VllmMlxVideoMultimodalBackend.requires.binaries == ["vllm-mlx"]
    assert VllmMlxVideoMultimodalBackend.requires.min_memory_gb == 12.0


def test_find_binary_returns_none_or_path() -> None:
    result = find_vllm_mlx_binary()
    assert result is None or Path(result).exists()


def test_frame_timestamp_uniform() -> None:
    md = {"fps": 2.0}
    # original index 4 @ 2 fps = 2.0s → 00:02
    assert _frame_timestamp(md, position=0, original_idx=4) == "00:02"
    # index 130 @ 1 fps = 130s → 02:10
    assert _frame_timestamp({"fps": 1.0}, position=0, original_idx=130) == "02:10"


def test_frame_timestamp_scene_midpoints() -> None:
    md = {"scene_midpoints_sec": [3.0, 65.0], "fps": 1.0}
    assert _frame_timestamp(md, position=0, original_idx=0) == "00:03"
    assert _frame_timestamp(md, position=1, original_idx=0) == "01:05"


def test_build_messages_shape(engine: Engine, tmp_path: Path) -> None:
    # Persist two fake frame jpgs into the content store.
    fid_a = "a" * 64
    fid_b = "b" * 64
    for fid in (fid_a, fid_b):
        src = tmp_path / f"{fid[:6]}.jpg"
        src.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
        engine.storage.store_file(src, fid, ".jpg")

    fs = FrameSet(
        id="f" * 64,
        path=tmp_path / "fs.json",
        metadata={
            "frame_ids": [fid_a, fid_b],
            "original_indices": [0, 5],
            "fps": 1.0,
        },
        created_at=datetime.now(UTC),
    )
    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("vm"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
    )
    params = MultimodalVideoParams(prompt="What is shown?", system_prompt="You are X")
    messages = _build_messages(ctx, fs, params)

    assert messages[0] == {"role": "system", "content": "You are X"}
    user = messages[1]
    assert user["role"] == "user"
    content = user["content"]
    # [Frame at 00:00] img [Frame at 00:05] img <prompt>
    labels = [c["text"] for c in content if c["type"] == "text"]
    assert "[Frame at 00:00]" in labels
    assert "[Frame at 00:05]" in labels
    assert "What is shown?" in labels
    images = [c for c in content if c["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_build_messages_no_system_prompt(engine: Engine, tmp_path: Path) -> None:
    fid = "c" * 64
    src = tmp_path / "c.jpg"
    src.write_bytes(b"\xff\xd8jpeg")
    engine.storage.store_file(src, fid, ".jpg")
    fs = FrameSet(
        id="g" * 64, path=tmp_path / "fs.json",
        metadata={"frame_ids": [fid], "original_indices": [0], "fps": 1.0},
        created_at=datetime.now(UTC),
    )
    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("vm2"),
        config=engine.config, storage=engine.storage,
    )
    messages = _build_messages(ctx, fs, MultimodalVideoParams(prompt="hi"))
    assert len(messages) == 1  # no system message
    assert messages[0]["role"] == "user"


def test_cost_estimate_is_local_only(tmp_path: Path) -> None:
    from media_engine.artifacts import Video

    b = VllmMlxVideoMultimodalBackend()
    v = Video(
        id="v" * 64, path=tmp_path / "v.mp4",
        metadata={"duration": 100.0}, created_at=datetime.now(UTC),
    )
    est = b.cost_estimate([v], MultimodalVideoParams(prompt="x"))
    assert est.cloud_cents == 0.0
    assert est.local_seconds > 0


async def test_execute_requires_run_op(engine: Engine, tmp_path: Path) -> None:
    from media_engine.artifacts import Video

    v = Video(
        id="v" * 64, path=tmp_path / "v.mp4",
        metadata={"duration": 5.0}, created_at=datetime.now(UTC),
    )
    bare_ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("noop"),
        config=engine.config,
        storage=engine.storage,
        # run_op deliberately None
    )
    with pytest.raises(RuntimeError, match="ctx.run_op"):
        await VllmMlxVideoMultimodalBackend().execute(
            [v], MultimodalVideoParams(prompt="x"), bare_ctx
        )


async def test_execute_end_to_end_mocked(
    engine: Engine, sample_mp4: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full orchestration with a fake ServerManager + mocked httpx POST."""
    from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams

    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("vm-e2e"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
        run_op=engine.run,
    )
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx
    )
    engine.cache.upsert_artifact(video)

    import media_engine.backends.video_multimodal.vllm_mlx as mod

    # Pretend the binary exists + the server is already healthy.
    monkeypatch.setattr(mod, "find_vllm_mlx_binary", lambda: "/usr/bin/true")

    class _FakeHealth:
        running = True
        healthy = True
        model = "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"

    monkeypatch.setattr(
        engine.server_manager, "health_check",
        lambda *a, **k: _FakeHealth(),
    )

    class _FakeResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": "A mocked description."}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 20,
                          "total_tokens": 520},
            }

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            pass

        async def post(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)

    [analysis] = await VllmMlxVideoMultimodalBackend().execute(
        [video],
        MultimodalVideoParams(
            prompt="Describe.",
            model="mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
        ),
        ctx,
    )
    assert analysis.data["text"] == "A mocked description."
    assert analysis.metadata["usage"]["input_tokens"] == 500
    assert analysis.metadata["usage"]["cost_cents"] == 0.0
    assert analysis.metadata["backend"] == "vllm-mlx"


@pytest.mark.needs_vllm
@pytest.mark.skipif(not VLLM_AVAILABLE, reason="vllm-mlx binary not found")
async def test_real_vllm_smoke(engine: Engine, sample_mp4: Path) -> None:
    from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams

    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("vm-real"),
        config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
        run_op=engine.run,
    )
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx
    )
    engine.cache.upsert_artifact(video)
    [analysis] = await engine.run(
        "video.multimodal",
        inputs=[video.id],
        prompt="Describe in one sentence.",
        model="mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
    )
    assert len(analysis.data["text"]) > 0
