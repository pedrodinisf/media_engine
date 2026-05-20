"""Tests for ops/search/semantic.py + the sqlite (brute-force) backend."""

from __future__ import annotations

import pytest

from media_engine.artifacts import Analysis, Embedding, Kind
from media_engine.backends import BackendRegistry
from media_engine.backends.search.sqlite import _cosine
from media_engine.ops.search.semantic import OP_NAME, SearchSemantic
from media_engine.runtime.engine import Engine

from ._search_helpers import make_embedding, make_transcript


def test_op_class_attributes() -> None:
    assert SearchSemantic.name == "search.semantic"
    assert SearchSemantic.input_kinds == (Kind.Embedding,)
    assert SearchSemantic.variadic_inputs is True
    assert SearchSemantic.output_kinds == (Kind.Analysis,)
    assert SearchSemantic.default_backend == "sqlite"


def test_backend_registered() -> None:
    assert "sqlite" in BackendRegistry.for_op("search.semantic")


def test_cosine_basic() -> None:
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    # Mismatched dims / empty vectors don't crash, just score 0.
    assert _cosine([], [1.0]) == 0.0
    assert _cosine([1.0], [1.0, 0.0]) == 0.0


async def test_ranks_by_cosine(engine: Engine) -> None:
    t_a = make_transcript(engine, key="a", text="A")
    t_b = make_transcript(engine, key="b", text="B")
    # Source kind is "transcript" for both; we pick by vector similarity.
    make_embedding(engine, key="a", vector=[1.0, 0.0, 0.0], source=t_a)
    make_embedding(engine, key="b", vector=[0.0, 1.0, 0.0], source=t_b)
    # Query: an Embedding artifact that points at no source.
    q = make_embedding(engine, key="q-near-a", vector=[0.95, 0.05, 0.0])

    [analysis] = await engine.run(
        OP_NAME, inputs=[q.id], top_k=5
    )
    assert isinstance(analysis, Analysis)
    results = analysis.metadata["results"]
    assert results, "expected hits"
    # The Embedding closest to the query was built from t_a → ranks first.
    assert results[0]["artifact_id"] == t_a.id
    assert results[0]["kind"] == "transcript"


async def test_query_embedding_self_excluded(engine: Engine) -> None:
    """The query Embedding itself must not surface as a top hit."""
    t = make_transcript(engine, key="t1", text="x")
    make_embedding(engine, key="e1", vector=[1.0, 0.0], source=t)
    # The query Embedding is itself in the index — it would otherwise
    # win with cosine 1.0; the op explicitly skips it.
    q = make_embedding(engine, key="q", vector=[1.0, 0.0])

    [a] = await engine.run(OP_NAME, inputs=[q.id], top_k=5)
    ids = [r["embedding_id"] for r in a.metadata["results"]]
    assert q.id not in ids


async def test_kind_filter_narrows_results(engine: Engine) -> None:
    from ._search_helpers import make_document

    t = make_transcript(engine, key="t", text="t")
    d = make_document(engine, key="d", text="d")
    make_embedding(engine, key="for-t", vector=[1.0, 0.0], source=t)
    make_embedding(engine, key="for-d", vector=[1.0, 0.0], source=d)
    q = make_embedding(engine, key="q", vector=[1.0, 0.0])

    [a] = await engine.run(
        OP_NAME, inputs=[q.id], top_k=5, kind_filter=(Kind.Document,)
    )
    results = a.metadata["results"]
    assert results
    assert all(r["kind"] == "document" for r in results)
    assert results[0]["artifact_id"] == d.id


async def test_cache_hit_on_rerun(engine: Engine, mocker) -> None:
    t = make_transcript(engine, key="t", text="t")
    make_embedding(engine, key="for-t", vector=[1.0, 0.0], source=t)
    q = make_embedding(engine, key="q", vector=[1.0, 0.0])

    [a1] = await engine.run(OP_NAME, inputs=[q.id])
    spy = mocker.spy(SearchSemantic, "run")
    [a2] = await engine.run(OP_NAME, inputs=[q.id])
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_refresh_nonce_forces_new_run(engine: Engine) -> None:
    t = make_transcript(engine, key="t", text="t")
    make_embedding(engine, key="for-t", vector=[1.0, 0.0], source=t)
    q = make_embedding(engine, key="q", vector=[1.0, 0.0])

    [a1] = await engine.run(OP_NAME, inputs=[q.id])
    [a2] = await engine.run(OP_NAME, inputs=[q.id], refresh_nonce="r2")
    assert a1.id != a2.id


async def test_rejects_wrong_kind(engine: Engine, sample_mp4) -> None:
    from media_engine.ops import OperationContext
    from media_engine.ops.acquire.upload import (
        AcquireUpload,
        AcquireUploadParams,
    )

    workdir = engine.storage.ensure_workdir("t")
    ctx = OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )
    [v] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx
    )
    engine.cache.upsert_artifact(v)
    with pytest.raises(ValueError, match="input kind mismatch"):
        await engine.run(OP_NAME, inputs=[v.id])


def test_pack_unpack_round_trip() -> None:
    from media_engine.backends.search.sqlite import _pack, _unpack

    v = [1.5, -2.25, 0.0, 7.125]
    blob = _pack(v)
    assert _unpack(blob, dims=len(v)) == pytest.approx(v)


async def test_empty_query_vector_raises(engine: Engine) -> None:
    """A zero-dim Embedding can't possibly score anything."""
    # Construct an Embedding with empty vector directly.
    from datetime import UTC, datetime

    from media_engine.artifacts import compute_derived_artifact_id

    derived_id = compute_derived_artifact_id(
        kind=Kind.Embedding,
        op_name="_test.empty",
        op_version="1",
        backend_name=None,
        backend_version=None,
        params={"k": "empty"},
        input_ids=[],
    )
    workdir = engine.storage.ensure_workdir("empty")
    tmp = workdir / "e.json"
    tmp.write_text('{"vector": [], "model": "none"}')
    dest = engine.storage.store_file(tmp, derived_id, ".json")
    art = Embedding(
        id=derived_id, path=dest,
        metadata={"vector": [], "model": "none"},
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(art)

    with pytest.raises(ValueError, match="no vector"):
        await engine.run(OP_NAME, inputs=[art.id])
