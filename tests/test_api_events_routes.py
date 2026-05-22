"""Phase 6 commit 43 — global events + ``?token=`` query-param auth shim.

Covers the additive surface:
- ``GET /events/history`` paginated tail of the persisted ``events`` table.
- ``?token=`` query-param accepted on every authed route (so browser
  ``EventSource`` can authenticate without custom headers).

The SSE streams themselves are integration-tested via the existing
``test_sse_stream_filters_by_job_id`` in ``tests/test_api.py``; this
file pins the query-param + global-history shapes.
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
def token_secret(api_engine: Engine) -> str:
    return create_token(api_engine.cache, label="test").secret


@pytest.fixture
def auth(token_secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_secret}"}


# ─────────────────────────────────────────────────────────────────
# ?token= query-param shim
# ─────────────────────────────────────────────────────────────────


def test_query_token_authenticates_when_header_missing(
    client: TestClient, token_secret: str
) -> None:
    """Browser EventSource workaround — equivalent to Bearer header."""
    r = client.get(f"/operations?token={token_secret}")
    assert r.status_code == 200


def test_query_token_overridden_by_invalid_header(
    client: TestClient, token_secret: str
) -> None:
    """Bearer header takes precedence; a valid query token doesn't rescue a bad header."""
    r = client.get(
        f"/operations?token={token_secret}",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401


def test_no_token_anywhere_returns_401(client: TestClient) -> None:
    r = client.get("/operations")
    assert r.status_code == 401


def test_invalid_query_token_returns_401(client: TestClient) -> None:
    r = client.get("/operations?token=nope")
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────
# GET /events/history
# ─────────────────────────────────────────────────────────────────


def test_events_history_shape_when_empty(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/events/history", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "limit" in body
    assert body["items"] == []


def test_events_history_rejects_bad_since(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/events/history?since=not-an-iso-timestamp", headers=auth)
    assert r.status_code == 400


def test_events_history_requires_token(client: TestClient) -> None:
    r = client.get("/events/history")
    assert r.status_code == 401


def test_events_history_limit_validated(
    client: TestClient, auth: dict[str, str]
) -> None:
    """limit must be 1..2000 per Query(ge=1, le=2000)."""
    r = client.get("/events/history?limit=0", headers=auth)
    assert r.status_code == 422
    r = client.get("/events/history?limit=99999", headers=auth)
    assert r.status_code == 422
    r = client.get("/events/history?limit=10", headers=auth)
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────
# /events/stream auth — verified indirectly via the routes that
# share `require_token`. The stream itself stays open indefinitely
# (per design), so TestClient consumption would deadlock; the unit
# test for `?token=` on `/operations` above is the canonical proof
# of the auth contract since /events/stream uses the same dependency.
# ─────────────────────────────────────────────────────────────────


def test_events_stream_rejects_missing_token(client: TestClient) -> None:
    """No header + no query token → 401, same as every other route."""
    # An unauthenticated SSE request short-circuits before the stream
    # opens, so this returns immediately.
    r = client.get("/events/stream")
    assert r.status_code == 401
