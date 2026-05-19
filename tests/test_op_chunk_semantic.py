"""Tests for ops/chunk/semantic.py + the default backend."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_engine.artifacts import Chunks, Kind, Transcript
from media_engine.backends.chunk_semantic.default import (
    _pack,
    _split_units,
)
from media_engine.ops.chunk.semantic import ChunkSemantic, ChunkSemanticParams
from media_engine.runtime.engine import Engine


def test_op_class_attributes() -> None:
    assert ChunkSemantic.name == "chunk.semantic"
    assert ChunkSemantic.input_kinds == (Kind.Transcript, Kind.MarkdownArtifact)
    assert ChunkSemantic.variadic_inputs is True
    assert ChunkSemantic.output_kinds == (Kind.Chunks,)
    assert ChunkSemantic.default_backend == "default"


def test_split_units_sentence() -> None:
    text = "Hello world. This is a test! How are you? Fine."
    units = _split_units(text, "sentence")
    assert len(units) == 4
    assert units[0] == "Hello world."


def test_split_units_paragraph() -> None:
    text = "Para one.\n\nPara two.\n\nPara three."
    units = _split_units(text, "paragraph")
    assert len(units) == 3


def test_pack_short_text_one_chunk() -> None:
    units = ["Sentence one.", "Sentence two."]
    chunks = _pack(units, max_chars=200, overlap_chars=20)
    assert len(chunks) == 1
    assert "Sentence one." in chunks[0]["text"]
    assert "Sentence two." in chunks[0]["text"]


def test_pack_overflow_creates_multiple() -> None:
    units = [f"Sentence number {i}." for i in range(20)]
    chunks = _pack(units, max_chars=80, overlap_chars=20)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c["text"]) <= 200  # cap + overlap allowance
    indices = [c["chunk_index"] for c in chunks]
    assert indices == list(range(len(chunks)))


def test_pack_empty_returns_empty() -> None:
    assert _pack([], max_chars=100, overlap_chars=10) == []


async def _persist_transcript(engine: Engine, text: str, segments=None) -> Transcript:
    t = Transcript(
        id="t" * 64,
        path=engine.config.permanent_store / "fake.json",
        metadata={
            "text": text,
            "segments": segments or [],
            "language": "en",
        },
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(t)
    return t


async def test_chunk_via_engine_short_text(engine: Engine) -> None:
    t = await _persist_transcript(engine, "Hi there. How are you doing today?")
    [chunks] = await engine.run("chunk.semantic", inputs=[t.id], max_chars=200)
    assert isinstance(chunks, Chunks)
    assert chunks.derived_from == (t.id,)
    assert len(chunks.chunks) == 1


async def test_chunk_via_engine_long_text_splits(engine: Engine) -> None:
    long_text = " ".join([f"Sentence number {i}." for i in range(50)])
    t = await _persist_transcript(engine, long_text)
    [chunks] = await engine.run(
        "chunk.semantic", inputs=[t.id], max_chars=100, overlap_chars=20,
    )
    assert len(chunks.chunks) > 1


async def test_chunk_uses_segments_when_present(engine: Engine) -> None:
    segs = [
        {"start": 0, "end": 1, "text": "First."},
        {"start": 1, "end": 2, "text": "Second."},
    ]
    t = await _persist_transcript(engine, text="", segments=segs)
    [chunks] = await engine.run("chunk.semantic", inputs=[t.id])
    body = chunks.chunks[0]["text"]
    assert "First" in body and "Second" in body


async def test_chunk_cache_hit(engine: Engine) -> None:
    t = await _persist_transcript(engine, "One. Two. Three.")
    [c1] = await engine.run("chunk.semantic", inputs=[t.id])
    [c2] = await engine.run("chunk.semantic", inputs=[t.id])
    assert c1.id == c2.id


async def test_chunk_param_change_yields_new_id(engine: Engine) -> None:
    t = await _persist_transcript(engine, "One. Two. Three.")
    [a] = await engine.run("chunk.semantic", inputs=[t.id], max_chars=100)
    [b] = await engine.run("chunk.semantic", inputs=[t.id], max_chars=200)
    assert a.id != b.id


async def test_chunk_rejects_non_text(engine: Engine, sample_mp4: Path) -> None:
    from media_engine.ops import OperationContext
    from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams

    op = AcquireUpload()
    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("t"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )
    [v] = await op.run([], AcquireUploadParams(source_path=sample_mp4), ctx)
    engine.cache.upsert_artifact(v)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("chunk.semantic", inputs=[v.id])


def test_params_defaults() -> None:
    p = ChunkSemanticParams()
    assert p.max_chars == 2000
    assert p.overlap_chars == 200
    assert p.strategy == "sentence"
