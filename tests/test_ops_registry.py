"""Tests for the Operation protocol + registry."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Audio, Kind, Video
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    OpRegistry,
    register_op,
)


def _now() -> datetime:
    return datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


class _ExtractAudioParams(BaseModel):
    sample_rate: int = 16000


class _DummyExtractAudio(Operation):
    name = "video.extract_audio"
    version = "1.0.0"
    input_kinds = (Kind.Video,)
    output_kinds = (Kind.Audio,)
    params_model = _ExtractAudioParams
    declared_resources = ()

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        # not exercised in this commit
        return []

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.5)


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    OpRegistry.clear()


def test_register_and_lookup() -> None:
    register_op(_DummyExtractAudio)
    cls = OpRegistry.get("video.extract_audio")
    assert cls is _DummyExtractAudio


def test_lookup_missing_raises() -> None:
    with pytest.raises(LookupError, match="No operation"):
        OpRegistry.get("never.registered")


def test_register_invalid_name_raises() -> None:
    class _BadName(_DummyExtractAudio):
        name = "no_dot_here"

    with pytest.raises(ValueError, match="<group>.<verb>"):
        register_op(_BadName)


def test_register_invalid_caps_raises() -> None:
    class _Caps(_DummyExtractAudio):
        name = "Video.ExtractAudio"

    with pytest.raises(ValueError, match="<group>.<verb>"):
        register_op(_Caps)


def test_register_too_many_dots_raises() -> None:
    class _TooManyDots(_DummyExtractAudio):
        name = "a.b.c"

    with pytest.raises(ValueError, match="<group>.<verb>"):
        register_op(_TooManyDots)


def test_double_registration_same_class_is_idempotent() -> None:
    register_op(_DummyExtractAudio)
    register_op(_DummyExtractAudio)
    assert OpRegistry.get("video.extract_audio") is _DummyExtractAudio


def test_double_registration_different_class_raises() -> None:
    register_op(_DummyExtractAudio)

    class _Other(_DummyExtractAudio):
        pass

    with pytest.raises(ValueError, match="already registered"):
        register_op(_Other)


def test_list_all_sorted_by_name() -> None:
    class _ExtractAudio(_DummyExtractAudio):
        name = "video.extract_audio"

    class _Transcribe(_DummyExtractAudio):
        name = "audio.transcribe"
        input_kinds = (Kind.Audio,)
        output_kinds = (Kind.Audio,)

    register_op(_Transcribe)
    register_op(_ExtractAudio)
    names = [op.name for op in OpRegistry.list_all()]
    assert names == ["audio.transcribe", "video.extract_audio"]


def test_has_returns_bool() -> None:
    register_op(_DummyExtractAudio)
    assert OpRegistry.has("video.extract_audio") is True
    assert OpRegistry.has("does.not_exist") is False


def test_op_subclass_attributes_visible(tmp_path: Path) -> None:
    op = _DummyExtractAudio()
    inputs: list[AnyArtifact] = [
        Video(id="v" * 8, path=tmp_path / "v.mp4", created_at=_now())
    ]
    est = op.cost_estimate(inputs, _ExtractAudioParams())
    assert est.local_seconds == 0.5
    assert _DummyExtractAudio.input_kinds == (Kind.Video,)
    assert _DummyExtractAudio.output_kinds == (Kind.Audio,)


def test_op_kinds_match_artifact_subclass(tmp_path: Path) -> None:
    """Sanity: declared input/output kinds line up with concrete subclass kinds."""
    v = Video(id="v" * 8, path=tmp_path / "v.mp4", created_at=_now())
    a = Audio(id="a" * 8, path=tmp_path / "a.wav", created_at=_now())
    assert v.kind in _DummyExtractAudio.input_kinds
    assert a.kind in _DummyExtractAudio.output_kinds
