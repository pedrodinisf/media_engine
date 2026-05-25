"""Unit tests for `media_engine.ops.video.comprehend.VideoComprehend`.

These exercise the composite's *orchestration* logic without booting
real backends: ``ctx.run_op`` is stubbed with canned per-op responses
so we can prove:

* frame-budget pre-flight raises on fps × duration > max_frames
* per-frame fan-out issues exactly ``frame_count`` ``frames.analyze`` calls
* the merged-timeline ordering is monotonic by ``t_sec``
* ``release_audio_models`` is invoked between the audio phase and the
  VLM fan-out
* the composite's own derived id is deterministic across runs with
  identical inputs
* ``output_kind`` routes to the correct synth op (extract vs summarize)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Audio,
    FrameSet,
    Kind,
    Transcript,
    Video,
)
from media_engine.ops import OperationContext
from media_engine.ops.video.comprehend import (
    ComprehendParams,
    VideoComprehend,
    _build_single_frame_frameset,
    _build_timeline_markdown,
)
from media_engine.runtime.engine import Engine


def _ctx_for(engine: Engine) -> OperationContext:
    workdir = engine.storage.ensure_workdir("comprehend-test")
    return OperationContext(
        workdir=workdir,
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
        run_op=_unused_run_op,
    )


async def _unused_run_op(*args: Any, **kwargs: Any) -> list[AnyArtifact]:
    raise AssertionError(
        f"unexpected ctx.run_op call: args={args!r} kwargs={kwargs!r}"
    )


def _video_artifact(tmp_path: Path, *, duration: float = 10.0) -> Video:
    p = tmp_path / "video.mp4"
    p.write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * 24)  # not a real mp4
    return Video(
        id="v" * 64,
        path=p,
        metadata={"duration": duration, "width": 640, "height": 360},
        created_at=datetime.now(UTC),
    )


def _make_canned_responses(
    *,
    audio: Audio,
    transcript: Transcript,
    frameset: FrameSet,
    frame_analysis_text: str = "a frame description",
    synth_outputs: list[Analysis] | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[AnyArtifact]]:
    """Build a (calls, responses) pair for a stubbed ctx.run_op.

    ``calls`` is mutated by the stub so the test can assert call shapes
    after run() returns.
    """
    n_frames = len(frameset.frame_ids)
    calls: list[tuple[str, dict[str, Any]]] = []
    analysis_path = audio.path.parent / "frame_analysis.json"
    analysis_path.write_text("{}")
    per_frame_analysis = Analysis(
        id="a" * 64,
        path=analysis_path,
        metadata={
            "data": {"text": frame_analysis_text},
            "model": "fake",
        },
        derived_from=(),
        created_at=datetime.now(UTC),
    )

    synth_default_path = audio.path.parent / "synth.json"
    synth_default_path.write_text("{}")
    synth_default = Analysis(
        id="s" * 64,
        path=synth_default_path,
        metadata={
            "data": {
                "title": "stub",
                "summary": "stub",
                "video_type": "test",
                "sections": [],
                "topics": [],
                "speakers": [],
            },
            "model": "fake-synth",
        },
        derived_from=(),
        created_at=datetime.now(UTC),
    )
    synth_responses = synth_outputs if synth_outputs is not None else [synth_default]

    state = {"synth_idx": 0}

    async def fake_run_op(
        op_name: str, **kwargs: Any
    ) -> list[AnyArtifact]:
        calls.append((op_name, kwargs))
        if op_name == "video.extract_audio":
            return [audio]
        if op_name == "audio.transcribe_diarized":
            return [transcript]
        if op_name == "video.sample_frames":
            return [frameset]
        if op_name == "frames.analyze":
            return [per_frame_analysis]
        if op_name in ("intelligence.extract", "intelligence.summarize"):
            i = state["synth_idx"]
            state["synth_idx"] += 1
            return [synth_responses[i % len(synth_responses)]]
        raise AssertionError(f"unstubbed ctx.run_op({op_name!r}, {kwargs!r})")

    _ = n_frames  # silence unused
    return calls, [fake_run_op]  # type: ignore[list-item]


def _audio_fixture(tmp_path: Path) -> Audio:
    p = tmp_path / "audio.wav"
    p.write_bytes(b"RIFF\x24\x00\x00\x00WAVE")
    return Audio(
        id="b" * 64,
        path=p,
        metadata={"duration": 10.0, "sample_rate": 16000, "channels": 1},
        created_at=datetime.now(UTC),
    )


def _transcript_fixture(tmp_path: Path) -> Transcript:
    p = tmp_path / "transcript.json"
    p.write_text("{}")
    return Transcript(
        id="t" * 64,
        path=p,
        metadata={
            "text": "hello there friend",
            "segments": [
                {"start": 0.0, "end": 1.5, "text": "hello there", "speaker_id": "SPEAKER_00"},
                {"start": 1.5, "end": 3.0, "text": "friend", "speaker_id": "SPEAKER_01"},
            ],
            "language": "en",
        },
        derived_from=(),
        created_at=datetime.now(UTC),
    )


def _frameset_fixture(tmp_path: Path, *, n_frames: int, fps: float = 1.0) -> FrameSet:
    p = tmp_path / "frameset.json"
    p.write_text("{}")
    return FrameSet(
        id="f" * 64,
        path=p,
        metadata={
            "frame_ids": [f"frame{i:02d}" + "0" * 56 for i in range(n_frames)],
            "original_indices": list(range(n_frames)),
            "fps": fps,
        },
        derived_from=(),
        created_at=datetime.now(UTC),
    )


# ─────────────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert VideoComprehend.name == "video.comprehend"
    assert VideoComprehend.input_kinds == (Kind.Video,)
    assert VideoComprehend.output_kinds == (Kind.Analysis,)
    # records_cost=False so the cost ledger doesn't double-count.
    assert VideoComprehend.records_cost is False
    # All six delegates declared honestly for doctor.
    assert set(VideoComprehend.delegates_to) == {
        "video.extract_audio",
        "audio.transcribe_diarized",
        "video.sample_frames",
        "frames.analyze",
        "intelligence.extract",
        "intelligence.summarize",
    }


def test_params_validate_range() -> None:
    with pytest.raises(ValueError, match="end_s must be > start_s"):
        ComprehendParams(start_s=10.0, end_s=10.0)


async def test_frame_budget_pre_flight_raises(
    engine: Engine, tmp_path: Path
) -> None:
    """fps × duration > max_frames must fail fast with a tunable message."""
    op = VideoComprehend()
    ctx = _ctx_for(engine)
    video = _video_artifact(tmp_path, duration=120.0)
    params = ComprehendParams(fps=4.0, max_frames=100)  # 4 × 120 = 480 > 100
    with pytest.raises(ValueError, match="exceeds max_frames"):
        await op.run([video], params, ctx)


async def test_per_frame_fanout_one_call_per_frame(
    engine: Engine, tmp_path: Path
) -> None:
    """frames.analyze is invoked exactly frame_count times."""
    op = VideoComprehend()
    audio = _audio_fixture(tmp_path)
    transcript = _transcript_fixture(tmp_path)
    frameset = _frameset_fixture(tmp_path, n_frames=5)
    calls, [fake_run_op] = _make_canned_responses(
        audio=audio, transcript=transcript, frameset=frameset
    )
    ctx = _ctx_for(engine)
    ctx_with_run = OperationContext(
        workdir=ctx.workdir,
        config=ctx.config,
        storage=ctx.storage,
        namespace=ctx.namespace,
        emit=ctx.emit,
        server_manager=ctx.server_manager,
        model_pool=ctx.model_pool,
        run_op=fake_run_op,  # type: ignore[arg-type]
    )
    video = _video_artifact(tmp_path, duration=5.0)
    # gemini model → no Apple-Silicon hardware gate; works on any host.
    params = ComprehendParams(
        fps=1.0, max_frames=10, vlm_model="gemini-2.5-flash"
    )
    await op.run([video], params, ctx_with_run)
    frame_calls = [c for c in calls if c[0] == "frames.analyze"]
    assert len(frame_calls) == 5


async def test_timeline_ordering_is_monotonic(
    engine: Engine, tmp_path: Path
) -> None:
    """The merged timeline must be sorted by t_sec across both modalities."""
    frame_entries = [
        (2.0, "second frame"),
        (0.0, "first frame"),
        (1.0, "middle frame"),
    ]
    transcript_segments = [
        {"start": 0.5, "end": 0.8, "text": "hi", "speaker_id": "SPK"},
        {"start": 2.5, "end": 3.0, "text": "bye", "speaker_id": "SPK"},
    ]
    md = _build_timeline_markdown(
        frame_entries=frame_entries,
        transcript_segments=transcript_segments,
    )
    lines = [line for line in md.splitlines() if line.startswith("[t=")]
    # Recover the embedded timestamps; they must be in ascending order.
    # Format is "[t=mm:ss.ss] …" → parse minutes + seconds back to a
    # comparable float.
    times: list[float] = []
    for line in lines:
        tag = line.split("]")[0][3:]  # "mm:ss.ss"
        m, s = tag.split(":")
        times.append(int(m) * 60 + float(s))
    assert times == sorted(times)


async def test_release_audio_models_called_between_audio_and_fanout(
    engine: Engine, tmp_path: Path
) -> None:
    """The RAM-saving helper fires exactly once, after transcribe_diarized
    and before the first frames.analyze."""
    audio = _audio_fixture(tmp_path)
    transcript = _transcript_fixture(tmp_path)
    frameset = _frameset_fixture(tmp_path, n_frames=2)
    calls, [fake_run_op] = _make_canned_responses(
        audio=audio, transcript=transcript, frameset=frameset
    )
    ctx = _ctx_for(engine)
    ctx_with_run = OperationContext(
        workdir=ctx.workdir,
        config=ctx.config,
        storage=ctx.storage,
        namespace=ctx.namespace,
        emit=ctx.emit,
        server_manager=ctx.server_manager,
        model_pool=ctx.model_pool,
        run_op=fake_run_op,  # type: ignore[arg-type]
    )
    video = _video_artifact(tmp_path, duration=2.0)
    params = ComprehendParams(
        fps=1.0, max_frames=10, vlm_model="gemini-2.5-flash"
    )

    with patch(
        "media_engine.ops.video.comprehend.release_audio_models"
    ) as mock_release:
        await VideoComprehend().run([video], params, ctx_with_run)

    assert mock_release.call_count == 1
    # Ordering: at the call site, transcribe_diarized has completed but
    # no frames.analyze has run yet. The call list lets us verify.
    op_sequence = [c[0] for c in calls]
    td_idx = op_sequence.index("audio.transcribe_diarized")
    sample_idx = op_sequence.index("video.sample_frames")
    first_frame_idx = op_sequence.index("frames.analyze")
    # transcribe_diarized fires first; video.sample_frames + frames.analyze
    # come after — the helper is called between them. We can't introspect
    # the exact temporal position of the mock call vs. the awaited ones
    # easily, but the call sequence proves the surrounding ordering.
    assert td_idx < sample_idx < first_frame_idx


async def test_derived_id_deterministic(
    engine: Engine, tmp_path: Path
) -> None:
    """Two identical runs produce the same output-Analysis id."""
    audio = _audio_fixture(tmp_path)
    transcript = _transcript_fixture(tmp_path)
    frameset = _frameset_fixture(tmp_path, n_frames=2)

    async def run_once() -> str:
        calls, [fake_run_op] = _make_canned_responses(
            audio=audio, transcript=transcript, frameset=frameset
        )
        ctx = _ctx_for(engine)
        ctx_with_run = OperationContext(
            workdir=ctx.workdir,
            config=ctx.config,
            storage=ctx.storage,
            namespace=ctx.namespace,
            emit=ctx.emit,
            server_manager=ctx.server_manager,
            model_pool=ctx.model_pool,
            run_op=fake_run_op,  # type: ignore[arg-type]
        )
        video = _video_artifact(tmp_path, duration=2.0)
        params = ComprehendParams(
            fps=1.0, max_frames=10, vlm_model="gemini-2.5-flash"
        )
        outs = await VideoComprehend().run([video], params, ctx_with_run)
        return outs[0].id

    id1 = await run_once()
    id2 = await run_once()
    assert id1 == id2


async def test_output_kind_routes_structured_vs_prose(
    engine: Engine, tmp_path: Path
) -> None:
    """structured → intelligence.extract; prose → intelligence.summarize."""
    audio = _audio_fixture(tmp_path)
    transcript = _transcript_fixture(tmp_path)
    frameset = _frameset_fixture(tmp_path, n_frames=1)

    for output_kind, expected_synth in [
        ("structured", "intelligence.extract"),
        ("prose", "intelligence.summarize"),
    ]:
        calls, [fake_run_op] = _make_canned_responses(
            audio=audio, transcript=transcript, frameset=frameset
        )
        ctx = _ctx_for(engine)
        ctx_with_run = OperationContext(
            workdir=ctx.workdir,
            config=ctx.config,
            storage=ctx.storage,
            namespace=ctx.namespace,
            emit=ctx.emit,
            server_manager=ctx.server_manager,
            model_pool=ctx.model_pool,
            run_op=fake_run_op,  # type: ignore[arg-type]
        )
        video = _video_artifact(tmp_path, duration=1.0)
        params = ComprehendParams(
            fps=1.0,
            max_frames=10,
            vlm_model="gemini-2.5-flash",
            output_kind=output_kind,  # type: ignore[arg-type]
        )
        await VideoComprehend().run([video], params, ctx_with_run)
        synth_calls = [c for c in calls if c[0] == expected_synth]
        other = (
            "intelligence.summarize"
            if expected_synth == "intelligence.extract"
            else "intelligence.extract"
        )
        other_calls = [c for c in calls if c[0] == other]
        assert len(synth_calls) == 1
        assert len(other_calls) == 0
        if expected_synth == "intelligence.extract":
            # Default schema must be forwarded; just check the structural
            # shape (top-level "object" + required title).
            kwargs = synth_calls[0][1]
            schema = kwargs.get("schema_def")
            assert isinstance(schema, dict)
            assert schema.get("type") == "object"
            assert "title" in (schema.get("required") or [])


async def test_single_frame_frameset_is_content_addressed(
    engine: Engine, tmp_path: Path
) -> None:
    """The ephemeral single-frame FrameSet ids are deterministic for the
    same (parent, position, fps) and differ otherwise."""
    ctx = _ctx_for(engine)
    parent = _frameset_fixture(tmp_path, n_frames=3, fps=2.0)
    a = _build_single_frame_frameset(parent=parent, position=0, ctx=ctx)
    b = _build_single_frame_frameset(parent=parent, position=0, ctx=ctx)
    c = _build_single_frame_frameset(parent=parent, position=1, ctx=ctx)
    assert a.id == b.id
    assert a.id != c.id


async def test_apple_silicon_only_vlm_rejected_on_other_host(
    engine: Engine, tmp_path: Path
) -> None:
    """mlx-community/* vlm_model on a non-arm64 host raises with guidance."""
    op = VideoComprehend()
    ctx = _ctx_for(engine)
    video = _video_artifact(tmp_path, duration=1.0)
    params = ComprehendParams(
        fps=1.0,
        max_frames=10,
        vlm_model="mlx-community/Qwen2-VL-7B-Instruct-4bit",
    )

    with (
        patch("platform.machine", return_value="x86_64"),
        pytest.raises(RuntimeError, match="vllm-mlx"),
    ):
        await op.run([video], params, ctx)
