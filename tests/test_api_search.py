"""Phase 6 commit 46 — POST /search.

Bearer-gated sync catalog query. Wraps ``Engine.run("search.<mode>")``
without the job/SSE lifecycle so type-as-you-go feedback stays fast.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_engine.api.app import build_app
from media_engine.api.auth import create_token
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine

from ._search_helpers import make_document, make_transcript


@pytest.fixture
def api_engine(tmp_path: Path) -> Iterator[Engine]:
    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
    )
    with Engine.open_quick(cfg) as e:
        yield e


@pytest.fixture
def client(api_engine: Engine) -> Iterator[TestClient]:
    app = build_app(engine=api_engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(api_engine: Engine) -> dict[str, str]:
    secret = create_token(api_engine.cache, label="test").secret
    return {"Authorization": f"Bearer {secret}"}


def test_search_fulltext_returns_results(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    """Happy path: a known-hit query against a tiny corpus returns the target."""
    target = make_transcript(
        api_engine,
        key="t1",
        text="Climate change focuses on carbon emissions and policy.",
    )
    make_transcript(
        api_engine, key="t2", text="Cooking recipes for Italian pasta."
    )

    r = client.post(
        "/search",
        json={"mode": "fulltext", "query": "climate carbon", "top_k": 5},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "fulltext"
    assert body["query"] == "climate carbon"
    assert body["top_k"] == 5
    assert body["results"]
    assert body["results"][0]["artifact_id"] == target.id
    assert body["results"][0]["kind"] == "transcript"
    assert body["results"][0]["score"] > 0
    assert body["results"][0]["snippet"]


def test_search_fulltext_kind_filter_narrows(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    """``kind`` restricts hits to that artifact kind."""
    make_transcript(api_engine, key="t1", text="Apollo lunar mission engineering.")
    doc = make_document(
        api_engine, key="d1", text="Apollo lunar mission analysis.", title="Apollo"
    )

    r = client.post(
        "/search",
        json={
            "mode": "fulltext",
            "query": "Apollo lunar",
            "top_k": 10,
            "kind": "document",
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results
    assert all(row["kind"] == "document" for row in results)
    assert results[0]["artifact_id"] == doc.id


def test_search_empty_corpus_returns_empty_results(
    client: TestClient, auth: dict[str, str]
) -> None:
    """No artifacts in the cache → empty results, not a 500."""
    r = client.post(
        "/search",
        json={"mode": "fulltext", "query": "anything", "top_k": 5},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"] == []


def test_search_unknown_kind_400(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/search",
        json={"mode": "fulltext", "query": "x", "kind": "not-a-kind"},
        headers=auth,
    )
    assert r.status_code == 400
    assert "unknown kind" in r.text


def test_search_invalid_mode_422(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/search",
        json={"mode": "invalid", "query": "x"},
        headers=auth,
    )
    assert r.status_code == 422


def test_search_top_k_upper_bound_422(
    client: TestClient, auth: dict[str, str]
) -> None:
    """top_k must be 1..200 inclusive (plan §13 risk #6)."""
    r = client.post(
        "/search",
        json={"mode": "fulltext", "query": "x", "top_k": 500},
        headers=auth,
    )
    assert r.status_code == 422


def test_search_empty_query_422(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/search",
        json={"mode": "fulltext", "query": ""},
        headers=auth,
    )
    assert r.status_code == 422


def test_search_missing_required_fields_422(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.post("/search", json={"mode": "fulltext"}, headers=auth)
    assert r.status_code == 422


def test_search_requires_token(client: TestClient) -> None:
    r = client.post("/search", json={"mode": "fulltext", "query": "x"})
    assert r.status_code == 401


def test_search_semantic_without_extra_400(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic mode raises 400 with an install hint when ``sentence-
    transformers`` is missing (rather than 500-ing on ImportError)."""
    from media_engine.runtime import search_query

    def _raise(_cfg: object, _q: str) -> str:
        raise RuntimeError(
            "sentence-transformers is not installed — semantic / hybrid "
            "search needs it for query embedding. Install: uv sync --extra embed"
        )

    monkeypatch.setattr(search_query, "embed_query_string", _raise)
    # Also patch the imported reference in the routes module.
    from media_engine.api import routes

    monkeypatch.setattr(routes, "embed_query_string", _raise)

    r = client.post(
        "/search",
        json={"mode": "semantic", "query": "anything", "top_k": 5},
        headers=auth,
    )
    assert r.status_code == 400
    assert "sentence-transformers" in r.text


def test_search_namespace_scope(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    """A token bound to a different namespace can't see this namespace's hits."""
    make_transcript(api_engine, key="ns1", text="zephyr quetzal viridian")

    other = create_token(
        api_engine.cache, label="other", namespace="other-ns"
    ).secret
    r = client.post(
        "/search",
        json={"mode": "fulltext", "query": "zephyr"},
        headers={"Authorization": f"Bearer {other}"},
    )
    # Namespace mismatch with the engine's namespace is rejected upstream.
    assert r.status_code == 403

    # Same query through the correct namespace's token finds the doc.
    r2 = client.post(
        "/search",
        json={"mode": "fulltext", "query": "zephyr"},
        headers=auth,
    )
    assert r2.status_code == 200
    assert r2.json()["results"]
