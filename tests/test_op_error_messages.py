"""Regression — op-entry error messages name the actionable fix.

Audit findings F-010..F-013. The reference for "good actionable error"
is ``ops/audio/detect_language.py:90`` and ``ops/search/semantic.py:63``
which both append "; register one or pass `backend=` to Engine.run."
to the "no default backend" RuntimeError. Three sibling ops omitted
the hint, and transcript.parse's "unknown format" didn't list the
valid formats. This test pins the hints so they don't drift back.
"""
from __future__ import annotations

import asyncio

import pytest

from media_engine.ops._base import OperationContext
from media_engine.ops.audio.diarize import AudioDiarize, DiarizeParams
from media_engine.ops.chunk.semantic import ChunkSemantic, ChunkSemanticParams
from media_engine.ops.embed.text import EmbedText, EmbedTextParams
from media_engine.ops.transcript.parse import _parse
from media_engine.runtime.engine import Engine


def _ctx(engine: Engine, slug: str) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir(slug),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=lambda _e: None,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )


def _check_backend_hint(message: str) -> None:
    assert "no default backend" in message
    assert "pass `backend=`" in message, (
        f"error message missing the `backend=` hint: {message!r}"
    )


def test_chunk_semantic_no_backend_error_names_fix(engine: Engine) -> None:
    """F-010 — chunk.semantic raises with the `backend=` hint."""
    op = ChunkSemantic()
    op.default_backend = None  # type: ignore[assignment]
    from datetime import UTC, datetime

    from media_engine.artifacts import MarkdownArtifact

    md = MarkdownArtifact(
        id="m" * 64,
        path=engine.storage.ensure_workdir("c").parent / "fake.md",
        metadata={"text": "x"},
        derived_from=(),
        created_at=datetime.now(UTC),
    )
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(op.run([md], ChunkSemanticParams(), _ctx(engine, "c1")))
    _check_backend_hint(str(exc.value))


def test_embed_text_no_backend_error_names_fix(engine: Engine) -> None:
    """F-011 — embed.text raises with the `backend=` hint."""
    op = EmbedText()
    op.default_backend = None  # type: ignore[assignment]
    from datetime import UTC, datetime

    from media_engine.artifacts import MarkdownArtifact

    md = MarkdownArtifact(
        id="e" * 64,
        path=engine.storage.ensure_workdir("e").parent / "fake.md",
        metadata={"text": "x"},
        derived_from=(),
        created_at=datetime.now(UTC),
    )
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(op.run([md], EmbedTextParams(), _ctx(engine, "e1")))
    _check_backend_hint(str(exc.value))


def test_audio_diarize_no_backend_error_names_fix(engine: Engine) -> None:
    """F-012 — audio.diarize raises with the `backend=` hint."""
    op = AudioDiarize()
    op.default_backend = None  # type: ignore[assignment]
    from datetime import UTC, datetime

    from media_engine.artifacts import Audio

    a = Audio(
        id="a" * 64,
        path=engine.storage.ensure_workdir("d").parent / "fake.wav",
        metadata={"duration": 1.0},
        derived_from=(),
        created_at=datetime.now(UTC),
    )
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(op.run([a], DiarizeParams(), _ctx(engine, "d1")))
    _check_backend_hint(str(exc.value))


def test_transcript_parse_unknown_format_lists_valid_formats() -> None:
    """F-013 — transcript.parse's unknown-format error names srt/vtt/speakered_txt."""
    with pytest.raises(ValueError) as exc:
        _parse("totally-bogus-format", "")  # type: ignore[arg-type]
    msg = str(exc.value)
    assert "srt" in msg
    assert "vtt" in msg
    assert "speakered_txt" in msg
