"""Tests for ops/search/fulltext.py + the sqlite-fts5 backend."""

from __future__ import annotations

import pytest

from media_engine.artifacts import Analysis, Kind
from media_engine.backends import BackendRegistry
from media_engine.ops.search.fulltext import (
    OP_NAME,
    SearchFulltext,
    SearchFulltextParams,
)
from media_engine.runtime.engine import Engine

from ._search_helpers import make_document, make_transcript, make_webpage


def test_op_class_attributes() -> None:
    assert SearchFulltext.name == "search.fulltext"
    assert SearchFulltext.input_kinds == ()
    assert SearchFulltext.output_kinds == (Kind.Analysis,)
    assert SearchFulltext.default_backend == "sqlite-fts5"


def test_backend_registered() -> None:
    assert "sqlite-fts5" in BackendRegistry.for_op("search.fulltext")


def test_cost_estimate_is_positive() -> None:
    est = SearchFulltext().cost_estimate(
        [], SearchFulltextParams(query="x")
    )
    assert est.local_seconds > 0


async def test_ranking_matches_known_corpus(engine: Engine) -> None:
    t_target = make_transcript(
        engine,
        key="target",
        text="Climate change discussion focuses on carbon emissions and policy.",
    )
    t_noise = make_transcript(
        engine,
        key="noise",
        text="Cooking recipe for traditional Italian pasta.",
    )
    make_document(
        engine,
        key="paper",
        text="Quantum entanglement paper on superposition states.",
        title="QM paper",
    )

    [analysis] = await engine.run(
        OP_NAME, query="climate carbon policy", top_k=5
    )
    assert isinstance(analysis, Analysis)
    results = analysis.metadata["results"]
    assert results, "expected at least one hit"
    assert results[0]["artifact_id"] == t_target.id
    # Cooking transcript should not be the top hit for a climate query.
    assert results[0]["artifact_id"] != t_noise.id
    assert results[0]["kind"] == "transcript"
    assert results[0]["snippet"]


async def test_kind_filter_narrows_results(engine: Engine) -> None:
    make_transcript(
        engine,
        key="t1",
        text="Apollo lunar mission engineering report.",
    )
    d = make_document(
        engine,
        key="d1",
        text="Apollo lunar mission detailed analysis.",
        title="Apollo PDF",
    )

    [a] = await engine.run(
        OP_NAME,
        query="Apollo lunar",
        top_k=10,
        kind_filter=(Kind.Document,),
    )
    results = a.metadata["results"]
    assert results
    assert all(r["kind"] == "document" for r in results)
    assert results[0]["artifact_id"] == d.id


async def test_cache_hit_on_rerun(engine: Engine, mocker) -> None:
    make_transcript(engine, key="t1", text="ducks geese swans waterfowl")
    [a1] = await engine.run(OP_NAME, query="ducks")
    spy = mocker.spy(SearchFulltext, "run")
    [a2] = await engine.run(OP_NAME, query="ducks")
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_refresh_nonce_forces_new_run(engine: Engine, mocker) -> None:
    make_transcript(engine, key="t1", text="ducks geese swans waterfowl")
    [a1] = await engine.run(OP_NAME, query="ducks")
    [a2] = await engine.run(OP_NAME, query="ducks", refresh_nonce="r2")
    assert a1.id != a2.id


async def test_query_param_change_yields_new_id(engine: Engine) -> None:
    make_transcript(engine, key="t1", text="alpha beta gamma delta")
    [a] = await engine.run(OP_NAME, query="alpha")
    [b] = await engine.run(OP_NAME, query="gamma")
    assert a.id != b.id


async def test_rejects_inputs(engine: Engine, sample_mp4) -> None:
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
    with pytest.raises(ValueError, match="expects no inputs"):
        await engine.run(OP_NAME, inputs=[v.id], query="x")


def test_query_escaping_isolates_fts_syntax() -> None:
    """User input must not be interpretable as FTS5 query syntax."""
    from media_engine.backends.search.sqlite_fts5 import _escape_fts_query

    # A stray double-quote would otherwise unbalance the FTS5 parser.
    escaped = _escape_fts_query('foo " bar')
    assert escaped.count('"') % 2 == 0
    # Empty input still yields a well-formed (no-op) query.
    assert _escape_fts_query("") == '""'


def test_artifact_text_extracts_per_kind(tmp_path) -> None:
    from datetime import UTC, datetime

    from media_engine.artifacts import (
        Chunks,
        MarkdownArtifact,
        Transcript,
    )
    from media_engine.backends.search._text import artifact_text

    md_path = tmp_path / "m.md"
    md_path.write_text("# Title\n\nMarkdown body content.")
    md = MarkdownArtifact(
        id="m" * 64,
        path=md_path,
        metadata={"title": "Title"},
        created_at=datetime.now(UTC),
    )
    assert "Markdown body content" in artifact_text(md)

    t = Transcript(
        id="t" * 64,
        path=tmp_path / "t.json",
        metadata={"segments": [{"text": "hello"}, {"text": "world"}]},
        created_at=datetime.now(UTC),
    )
    assert artifact_text(t) == "hello world"

    c = Chunks(
        id="c" * 64,
        path=tmp_path / "c.json",
        metadata={"chunks": [{"text": "a b"}, {"text": "c d"}]},
        created_at=datetime.now(UTC),
    )
    assert artifact_text(c) == "a b c d"


async def test_webpage_indexed(engine: Engine) -> None:
    page = make_webpage(
        engine, key="w1", url="https://x.example/p",
        text="Forge tariffs negotiation summit communique.",
    )
    [a] = await engine.run(OP_NAME, query="tariffs negotiation")
    results = a.metadata["results"]
    assert results
    assert results[0]["artifact_id"] == page.id
    assert results[0]["kind"] == "webpage"
