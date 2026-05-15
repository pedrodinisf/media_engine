"""Tests for the Backend protocol + BackendRegistry."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext


class _Params(BaseModel):
    pass


class _MlxWhisper(Backend):
    op_name = "audio.transcribe"
    name = "mlx-whisper"
    version = "1.0.0"
    requires = BackendRequirements(hardware=["apple_silicon"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        return []

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=10.0)


class _OpenaiWhisper(Backend):
    op_name = "audio.transcribe"
    name = "openai-whisper"
    version = "1.0.0"
    requires = BackendRequirements(env=["OPENAI_API_KEY"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        return []

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(cloud_cents=1.0)


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    BackendRegistry.clear()


def test_register_and_lookup() -> None:
    register_backend(_MlxWhisper)
    cls = BackendRegistry.get("audio.transcribe", "mlx-whisper")
    assert cls is _MlxWhisper


def test_register_two_backends_same_op() -> None:
    register_backend(_MlxWhisper)
    register_backend(_OpenaiWhisper)
    assert BackendRegistry.for_op("audio.transcribe") == ["mlx-whisper", "openai-whisper"]


def test_get_missing_raises_with_available_list() -> None:
    register_backend(_MlxWhisper)
    with pytest.raises(LookupError, match="mlx-whisper"):
        BackendRegistry.get("audio.transcribe", "does-not-exist")


def test_get_unknown_op_raises() -> None:
    with pytest.raises(LookupError, match="none"):
        BackendRegistry.get("never.heard", "x")


def test_double_registration_same_class_is_idempotent() -> None:
    register_backend(_MlxWhisper)
    register_backend(_MlxWhisper)
    assert BackendRegistry.get("audio.transcribe", "mlx-whisper") is _MlxWhisper


def test_double_registration_different_class_raises() -> None:
    register_backend(_MlxWhisper)

    class _Other(_MlxWhisper):
        pass

    with pytest.raises(ValueError, match="already registered"):
        register_backend(_Other)


def test_health_default_ok() -> None:
    assert _MlxWhisper.health() == "ok"


def test_for_op_returns_empty_when_unknown() -> None:
    assert BackendRegistry.for_op("never.heard") == []


def test_list_all_sorted() -> None:
    register_backend(_OpenaiWhisper)
    register_backend(_MlxWhisper)

    class _Diarize(_MlxWhisper):
        op_name = "audio.diarize"
        name = "pyannote"

    register_backend(_Diarize)
    sorted_names = [(b.op_name, b.name) for b in BackendRegistry.list_all()]
    assert sorted_names == [
        ("audio.diarize", "pyannote"),
        ("audio.transcribe", "mlx-whisper"),
        ("audio.transcribe", "openai-whisper"),
    ]


def test_backend_requirements_default_empty() -> None:
    req = BackendRequirements()
    assert req.env == []
    assert req.binaries == []
    assert req.min_memory_gb == 0.0


def test_backend_requirements_serializes() -> None:
    req = BackendRequirements(
        env=["HF_TOKEN"], binaries=["ffmpeg"], hardware=["apple_silicon"],
        min_memory_gb=8.0,
    )
    j = req.model_dump_json()
    assert "HF_TOKEN" in j
    assert "8.0" in j
