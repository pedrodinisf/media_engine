"""Tests for ops/search/hybrid.py — RRF fusion of semantic + fulltext."""

from __future__ import annotations

from media_engine.artifacts import Analysis, Kind
from media_engine.ops.search.hybrid import (
    OP_NAME,
    SearchHybrid,
    reciprocal_rank_fusion,
)
from media_engine.runtime.engine import Engine

from ._search_helpers import make_embedding, make_transcript


def test_op_class_attributes() -> None:
    assert SearchHybrid.name == "search.hybrid"
    assert SearchHybrid.input_kinds == (Kind.Embedding,)
    assert SearchHybrid.variadic_inputs is True
    assert SearchHybrid.output_kinds == (Kind.Analysis,)
    assert SearchHybrid.records_cost is False


# ─────────────────────────────────────────────────────────────────
# Pure RRF — boundary cases
# ─────────────────────────────────────────────────────────────────


def test_rrf_single_list_preserves_order() -> None:
    ranks = [
        [{"artifact_id": "a"}, {"artifact_id": "b"}, {"artifact_id": "c"}],
    ]
    out = reciprocal_rank_fusion(ranks, rrf_k=60)
    assert [r["artifact_id"] for r in out] == ["a", "b", "c"]
    assert out[0]["score"] > out[1]["score"] > out[2]["score"]


def test_rrf_two_lists_appearing_in_both_outscore_singletons() -> None:
    sem = [{"artifact_id": "a"}, {"artifact_id": "b"}]
    ft = [{"artifact_id": "b"}, {"artifact_id": "c"}]
    out = reciprocal_rank_fusion([sem, ft], rrf_k=60)
    by_id = {r["artifact_id"]: r["score"] for r in out}
    # "b" appears in both modalities → wins. "a" beats "c" on rank-1 vs
    # rank-2 in their sole modality.
    assert by_id["b"] > by_id["a"] > by_id["c"]
    assert [r["artifact_id"] for r in out] == ["b", "a", "c"]


def test_rrf_carries_kind_and_snippet_through() -> None:
    ranks = [
        [{"artifact_id": "a", "kind": "transcript", "snippet": "hi"}],
        [{"artifact_id": "a", "kind": "transcript", "snippet": "bye"}],
    ]
    [merged] = reciprocal_rank_fusion(ranks, rrf_k=60)
    assert merged["kind"] == "transcript"
    assert merged["snippet"] == "hi"  # first non-empty wins
    # Ranks tracked per modality for transparency.
    assert merged["ranks"] == {"0": 1, "1": 1}


def test_rrf_skips_malformed_entries() -> None:
    ranks = [
        [{"artifact_id": "a"}, {"no_id": True}, {"artifact_id": 42}],
    ]
    out = reciprocal_rank_fusion(ranks, rrf_k=60)
    assert [r["artifact_id"] for r in out] == ["a"]


# ─────────────────────────────────────────────────────────────────
# Engine.run integration — composite dispatch
# ─────────────────────────────────────────────────────────────────


async def test_hybrid_dispatches_both_sub_ops(engine: Engine) -> None:
    t = make_transcript(
        engine,
        key="tariffs",
        text="Tariffs and trade negotiations between major economies.",
    )
    make_embedding(engine, key="for-t", vector=[1.0, 0.0], source=t)
    q = make_embedding(engine, key="q", vector=[1.0, 0.0])

    [analysis] = await engine.run(
        OP_NAME, inputs=[q.id], query="tariffs trade", top_k=5
    )
    assert isinstance(analysis, Analysis)
    md = analysis.metadata
    assert md["mode"] == "hybrid"
    # The sub-op output ids are carried through for transparency.
    assert "semantic_id" in md["components"]
    assert "fulltext_id" in md["components"]
    # The transcript that matches both modalities ranks first.
    results = md["results"]
    assert results
    assert results[0]["artifact_id"] == t.id
    # Each fused hit carries the per-modality ranks dict.
    assert "ranks" in results[0]


async def test_hybrid_cache_hit_on_rerun(engine: Engine, mocker) -> None:
    t = make_transcript(engine, key="t", text="ducks geese swans")
    make_embedding(engine, key="for-t", vector=[1.0, 0.0], source=t)
    q = make_embedding(engine, key="q", vector=[1.0, 0.0])

    [a1] = await engine.run(
        OP_NAME, inputs=[q.id], query="ducks", top_k=5
    )
    spy = mocker.spy(SearchHybrid, "run")
    [a2] = await engine.run(
        OP_NAME, inputs=[q.id], query="ducks", top_k=5
    )
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_hybrid_param_change_yields_new_id(engine: Engine) -> None:
    t = make_transcript(engine, key="t", text="alpha beta")
    make_embedding(engine, key="for-t", vector=[1.0, 0.0], source=t)
    q = make_embedding(engine, key="q", vector=[1.0, 0.0])

    [a] = await engine.run(OP_NAME, inputs=[q.id], query="alpha")
    [b] = await engine.run(OP_NAME, inputs=[q.id], query="alpha", rrf_k=10)
    assert a.id != b.id
