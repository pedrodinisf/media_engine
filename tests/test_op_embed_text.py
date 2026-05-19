"""Tests for ops/embed/text.py + sentence-transformers backend.

Op-contract + dispatch tests run with a fake backend (always available).
The real sentence-transformers backend smoke-runs when the optional dep
is installed.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Chunks,
    Embedding,
    Kind,
    Transcript,
)
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.embed.text import (
    EmbedText,
    EmbedTextParams,
    build_embedding_artifact,
)
from media_engine.runtime.engine import Engine

ST_AVAILABLE = importlib.util.find_spec("sentence_transformers") is not None


def test_op_class_attributes() -> None:
    assert EmbedText.name == "embed.text"
    assert EmbedText.input_kinds == (
        Kind.Transcript, Kind.MarkdownArtifact, Kind.Chunks
    )
    assert EmbedText.variadic_inputs is True
    assert EmbedText.output_kinds == (Kind.Embedding,)
    assert EmbedText.declared_resources == ("apple_gpu",)
    assert EmbedText.default_backend == "sentence-transformers"


def test_params_defaults() -> None:
    p = EmbedTextParams()
    assert p.model == "sentence-transformers/all-MiniLM-L6-v2"
    assert p.normalize is True


@pytest.fixture
def fake_embed_backend() -> type[Backend]:
    BackendRegistry.unregister("embed.text", "sentence-transformers")

    @register_backend
    class _Fake(Backend):
        op_name = "embed.text"
        name = "sentence-transformers"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, EmbedTextParams)
            source = inputs[0]
            assert isinstance(source, Chunks | Transcript)
            # Deterministic 4-dim "vector" so tests are reproducible.
            if isinstance(source, Chunks):
                chunks = source.chunks
            else:
                chunks = [{"text": source.metadata.get("text", ""), "chunk_index": None}]
            out: list[AnyArtifact] = []
            for c in chunks:
                text = str(c.get("text", ""))
                idx = c.get("chunk_index")
                vector = [float(len(text)), 0.5, 0.25, 0.125]
                out.append(
                    build_embedding_artifact(
                        source=source,
                        params=params,
                        backend_name=self.name,
                        backend_version=self.version,
                        workdir_path=ctx.workdir,
                        storage=ctx.storage,
                        vector=vector,
                        chunk_text=text,
                        chunk_index=idx,
                    )
                )
            return out

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=0.5)

    yield _Fake
    BackendRegistry.unregister("embed.text", "sentence-transformers")
    # Restore the real backend for downstream tests.
    try:
        from media_engine.backends.embed_text.sentence_transformers import (
            SentenceTransformersEmbedTextBackend,
        )
        BackendRegistry.register(SentenceTransformersEmbedTextBackend)
    except ImportError:
        pass


async def _persist_chunks(engine: Engine, n: int) -> Chunks:
    payload = {
        "chunks": [
            {"text": f"chunk {i} text", "chunk_index": i, "char_start": 0, "char_end": 0}
            for i in range(n)
        ],
        "max_chars": 200,
        "overlap_chars": 20,
        "strategy": "sentence",
        "parent_artifact_id": "p" * 64,
    }
    c = Chunks(
        id=f"{n:064d}",
        path=engine.config.permanent_store / "fake_chunks.json",
        metadata=payload,
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(c)
    return c


async def test_embed_chunks_via_fake_backend(
    engine: Engine, fake_embed_backend
) -> None:
    chunks = await _persist_chunks(engine, 3)
    embeddings = await engine.run("embed.text", inputs=[chunks.id])
    assert len(embeddings) == 3
    for e in embeddings:
        assert isinstance(e, Embedding)
        assert e.dimensions == 4
        assert e.derived_from == (chunks.id,)


async def test_embed_transcript_one_vector(
    engine: Engine, fake_embed_backend
) -> None:
    t = Transcript(
        id="t" * 64,
        path=engine.config.permanent_store / "t.json",
        metadata={"text": "hello world", "segments": [], "language": "en"},
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(t)

    # Transcript reaches embed.text through Engine.run now that the op is
    # declared variadic over {Transcript, Markdown, Chunks}.
    embeddings = await engine.run("embed.text", inputs=[t.id])
    assert len(embeddings) == 1
    assert embeddings[0].dimensions == 4


async def test_embed_cache_hit(engine: Engine, fake_embed_backend) -> None:
    chunks = await _persist_chunks(engine, 2)
    e1 = await engine.run("embed.text", inputs=[chunks.id])
    e2 = await engine.run("embed.text", inputs=[chunks.id])
    assert {e.id for e in e1} == {e.id for e in e2}


async def test_embed_param_change_yields_new_ids(
    engine: Engine, fake_embed_backend
) -> None:
    chunks = await _persist_chunks(engine, 2)
    a = await engine.run("embed.text", inputs=[chunks.id], normalize=True)
    b = await engine.run("embed.text", inputs=[chunks.id], normalize=False)
    a_ids = {e.id for e in a}
    b_ids = {e.id for e in b}
    assert a_ids.isdisjoint(b_ids)


async def test_embed_rejects_wrong_kind(
    engine: Engine, sample_mp4, fake_embed_backend
) -> None:
    from media_engine.ops.acquire.upload import (
        AcquireUpload,
        AcquireUploadParams,
    )

    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("e"),
        config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx
    )
    engine.cache.upsert_artifact(video)
    with pytest.raises(ValueError, match="input kind mismatch"):
        await engine.run("embed.text", inputs=[video.id])


@pytest.mark.skipif(not ST_AVAILABLE, reason="sentence-transformers not installed")
async def test_real_sentence_transformers_smoke(engine: Engine) -> None:
    chunks = await _persist_chunks(engine, 2)
    embeddings = await engine.run(
        "embed.text",
        inputs=[chunks.id],
        model="sentence-transformers/all-MiniLM-L6-v2",
    )
    assert len(embeddings) == 2
    for e in embeddings:
        assert e.dimensions > 100  # MiniLM-L6 = 384
