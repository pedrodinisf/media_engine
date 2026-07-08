"""Tests for the pyannote ``speakers.embed_voice`` backend.

The real embedding path (``needs_pyannote`` + HF_TOKEN + a real fixture) is
skipped in CI; the array-flattening helper is pure and always runs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from media_engine.artifacts import Diarization, SpeakerEmbedding
from media_engine.backends import BackendRegistry
from media_engine.backends.embed_voice.pyannote import (
    PyannoteEmbedVoiceBackend,
    _as_1d,
)
from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.runtime.engine import Engine

try:
    import pyannote.audio  # type: ignore[import-not-found]  # noqa: F401
    PYANNOTE_AVAILABLE = True
except ImportError:
    PYANNOTE_AVAILABLE = False


def test_backend_registered_with_requirements() -> None:
    assert BackendRegistry.has("speakers.embed_voice", "pyannote")
    reqs = PyannoteEmbedVoiceBackend.requires
    assert reqs.env == ["HF_TOKEN"]
    assert reqs.hardware == ["apple_silicon"]


# ── _as_1d flattening (pure, no pyannote needed) ─────────────────────


def test_as_1d_plain_list() -> None:
    assert _as_1d([0.1, 0.2, 0.3]) == [0.1, 0.2, 0.3]


def test_as_1d_unwraps_row() -> None:
    assert _as_1d([[0.1, 0.2, 0.3]]) == [0.1, 0.2, 0.3]


class _FakeArray:
    def __init__(self, data):
        self.data = data

    def tolist(self):
        return self.data


def test_as_1d_from_arraylike_data_attr() -> None:
    assert _as_1d(_FakeArray([[0.4, 0.5]])) == [0.4, 0.5]


def _ctx_for(engine: Engine) -> OperationContext:
    workdir = engine.storage.ensure_workdir("test")
    return OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
    )


@pytest.mark.needs_pyannote
@pytest.mark.skipif(
    not (PYANNOTE_AVAILABLE and os.environ.get("HF_TOKEN")),
    reason="pyannote.audio + HF_TOKEN required",
)
async def test_real_embed_voice_produces_vectors(
    engine: Engine, sample_dialogue_wav: Path
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_dialogue_wav),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [diar] = await engine.run("audio.diarize", inputs=[audio.id])
    assert isinstance(diar, Diarization)

    [emb] = await engine.run(
        "speakers.embed_voice", inputs=[audio.id, diar.id]
    )
    assert isinstance(emb, SpeakerEmbedding)
    assert emb.dimensions and emb.dimensions > 0
    assert len(emb.turns) >= 1
    assert all(len(t["vector"]) == emb.dimensions for t in emb.turns)
