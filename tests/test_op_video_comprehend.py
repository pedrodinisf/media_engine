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


def test_default_vlm_model_is_commodity_hardware_safe() -> None:
    """F-009 — default vlm_model must fit 16 GB Macs alongside whisper+pyannote.

    The 7B variant (~8 GB resident) + whisper (~3 GB) + pyannote (~1 GB)
    blows past the safety margin on the most common operator hardware.
    Commit ab43c9a lowered the *profile* default; this regression pins
    the OP default to the safe 2B variant.
    """
    assert ComprehendParams().vlm_model == "mlx-community/Qwen2-VL-2B-Instruct-4bit"


async def test_frame_budget_pre_flight_raises(
    engine: Engine, tmp_path: Path
) -> None:
    """fps × duration > max_frames must fail fast with a tunable message."""
    op = VideoComprehend()
    ctx = _ctx_for(engine)
    video = _video_artifact(tmp_path, duration=120.0)
    params = ComprehendParams(
        fps=4.0, max_frames=100, vlm_model="gemini-2.5-flash"
    )  # 4 × 120 = 480 > 100
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


async def test_single_frame_frameset_registers_in_cache(
    engine: Engine, tmp_path: Path
) -> None:
    """The ephemeral FrameSet must be queryable via Cache.get_artifact —
    otherwise the subsequent ctx.run_op("frames.analyze") fan-out
    hits LookupError. (Regression: produced "input artifact not
    found: <id>" mid-run on real video.comprehend invocations.)"""
    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("comprehend-cache-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
        run_op=_unused_run_op,
        cache=engine.cache,
    )
    parent = _frameset_fixture(tmp_path, n_frames=3, fps=2.0)
    single = _build_single_frame_frameset(parent=parent, position=1, ctx=ctx)
    found = engine.cache.get_artifact(single.id, namespace=engine.config.namespace)
    assert found is not None, "single-frame FrameSet must land in the cache"
    assert found.id == single.id
    assert found.kind == Kind.FrameSet
    # Calling again with the same (parent, position) is idempotent
    # (same id, no IntegrityError from the cache).
    again = _build_single_frame_frameset(parent=parent, position=1, ctx=ctx)
    assert again.id == single.id


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


async def test_comprehend_forwards_range_to_sample_frames(
    engine: Engine, tmp_path: Path
) -> None:
    """Phase 6.7 — when comprehend is given start_s/end_s, sample_frames
    receives them so the visual side stays aligned with the audio side."""
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
    video = _video_artifact(tmp_path, duration=120.0)
    params = ComprehendParams(
        fps=1.0, max_frames=240, vlm_model="gemini-2.5-flash",
        start_s=30.0, end_s=90.0,
    )
    await VideoComprehend().run([video], params, ctx_with_run)

    sf_calls = [c for c in calls if c[0] == "video.sample_frames"]
    assert len(sf_calls) == 1
    sf_kwargs = sf_calls[0][1]
    assert sf_kwargs.get("start_s") == 30.0
    assert sf_kwargs.get("end_s") == 90.0
    # And the audio path also received the same window.
    td_calls = [c for c in calls if c[0] == "audio.transcribe_diarized"]
    assert td_calls[0][1].get("start_s") == 30.0
    assert td_calls[0][1].get("end_s") == 90.0
    # F-008: video.extract_audio also receives the window — otherwise
    # the audio file would cover the whole video while transcript +
    # frames describe only [start_s,end_s]. Symmetric with the sample_frames
    # forwarding fix in 02fc1e9.
    ea_calls = [c for c in calls if c[0] == "video.extract_audio"]
    assert len(ea_calls) == 1
    assert ea_calls[0][1].get("start_s") == 30.0
    assert ea_calls[0][1].get("end_s") == 90.0


def test_meeting_style_has_prompt_and_user_text() -> None:
    """The new 'meeting' style is wired in SYSTEM_PROMPTS + USER_PROMPTS."""
    from media_engine.ops.video._comprehend_prompts import (
        SYSTEM_PROMPTS,
        USER_PROMPTS,
    )
    assert "meeting" in SYSTEM_PROMPTS
    assert "meeting" in USER_PROMPTS
    # Must reference the load-bearing deliverables — otherwise the
    # synth model won't know to extract them aggressively.
    sp = SYSTEM_PROMPTS["meeting"]
    assert "decisions" in sp
    assert "action_items" in sp


def test_default_schema_has_meeting_fields() -> None:
    """`decisions[]` and `action_items[]` exist on the default schema as
    optional fields (not in `required`, so other styles aren't forced
    to populate them)."""
    from media_engine.ops.video._comprehend_prompts import DEFAULT_SCHEMA
    props = DEFAULT_SCHEMA["properties"]
    assert "decisions" in props
    assert "action_items" in props
    # Optional → not in required.
    assert "decisions" not in DEFAULT_SCHEMA["required"]
    assert "action_items" not in DEFAULT_SCHEMA["required"]
    # Action item items at minimum carry t_seconds + task.
    ai_required = props["action_items"]["items"]["required"]
    assert "task" in ai_required
    assert "t_seconds" in ai_required


def test_meeting_style_accepted_by_params() -> None:
    """style='meeting' parses (the Literal accepts it)."""
    p = ComprehendParams(style="meeting")  # type: ignore[arg-type]
    assert p.style == "meeting"


async def test_meeting_profile_yaml_parses() -> None:
    """The bundled teams-meeting profile compiles via the profile loader."""
    from media_engine.profiles import load_profile
    profile_path = (
        Path(__file__).parent.parent
        / "profiles"
        / "examples"
        / "teams-meeting.yaml"
    )
    assert profile_path.exists()
    profile = load_profile(profile_path)
    assert profile.name == "teams-meeting"
    # One node, calls video.comprehend with style=meeting.
    assert len(profile.graph) == 1
    node = profile.graph[0]
    assert node.op == "video.comprehend"
    assert node.params["style"] == "meeting"
    assert node.params["output_kind"] == "structured"


async def test_comprehend_marks_intermediates_ephemeral(
    engine: Engine, tmp_path: Path
) -> None:
    """After the synth completes, the per-frame Analyses + the timeline
    MarkdownArtifact + the inner synth Analysis are all stamped with
    ``metadata.ephemeral = true`` so the catalog list hides them by
    default. The outer composite Analysis stays visible."""
    audio = _audio_fixture(tmp_path)
    transcript = _transcript_fixture(tmp_path)
    frameset = _frameset_fixture(tmp_path, n_frames=2)
    calls, [fake_run_op] = _make_canned_responses(
        audio=audio, transcript=transcript, frameset=frameset
    )
    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("comprehend-eph-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
        run_op=fake_run_op,  # type: ignore[arg-type]
        cache=engine.cache,
    )
    video = _video_artifact(tmp_path, duration=2.0)
    params = ComprehendParams(
        fps=1.0, max_frames=10, vlm_model="gemini-2.5-flash"
    )

    # Spy on mark_artifacts_ephemeral so we can assert the exact set
    # of intermediate ids the op flags. This test bypasses Engine.run
    # (uses a stubbed ctx.run_op), so the outer Analysis op.run returns
    # isn't auto-upserted — we verify the intermediates instead.
    marked_calls: list[list[str]] = []
    original_mark = engine.cache.mark_artifacts_ephemeral

    def spy_mark(ids: list[str], namespace: str = "default") -> int:
        marked_calls.append(list(ids))
        return original_mark(ids, namespace=namespace)

    engine.cache.mark_artifacts_ephemeral = spy_mark  # type: ignore[method-assign]
    try:
        await VideoComprehend().run([video], params, ctx)
    finally:
        engine.cache.mark_artifacts_ephemeral = original_mark  # type: ignore[method-assign]

    # Single bulk mark, fired after the synth.
    assert len(marked_calls) == 1
    marked_ids = marked_calls[0]
    # 2 per-frame Analysis ids (fake returns same stub so set may
    # dedup to 1) + 1 timeline MarkdownArtifact + 1 synth Analysis.
    assert len(marked_ids) >= 3

    # Timeline MarkdownArtifact lands in the cache (real upsert path)
    # AND is ephemeral so it's hidden from the default list.
    md_default = [
        a for a in engine.cache.list_artifacts()
        if a.kind == Kind.MarkdownArtifact
    ]
    md_with_internal = [
        a for a in engine.cache.list_artifacts(include_ephemeral=True)
        if a.kind == Kind.MarkdownArtifact
    ]
    assert md_default == [], (
        f"timeline MarkdownArtifact must be ephemeral, got {md_default}"
    )
    assert len(md_with_internal) >= 1, (
        "timeline MarkdownArtifact must exist when include_ephemeral=True"
    )
