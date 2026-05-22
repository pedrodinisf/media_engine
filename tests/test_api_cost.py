"""Phase 6 commit 46 — cost ledger REST surface.

GET /cost/summary + GET /cost/log: read-side views over the
``cost_log`` table. Drives the Web UI's ``/ui/cost`` panel.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
def auth(api_engine: Engine) -> dict[str, str]:
    secret = create_token(api_engine.cache, label="test").secret
    return {"Authorization": f"Bearer {secret}"}


def _seed_rows(api_engine: Engine) -> None:
    """Write a tiny synthetic ledger so the routes have something to summarize."""
    now = datetime.now(UTC)
    rows = [
        # op_name, backend, cents, tokens_in, tokens_out, offset_minutes
        ("audio.transcribe", "mlx-whisper", 0.0, 0, 0, 5),
        ("audio.transcribe", "mlx-whisper", 0.0, 0, 0, 3),
        ("text.summarize", "openai", 12.5, 1024, 256, 2),
        ("text.summarize", "anthropic", 7.0, 512, 128, 1),
        ("frames.analyze", "gemini", 3.0, 200, 50, 0),
    ]
    for op, backend, cents, tin, tout, off in rows:
        api_engine.cache.record_cost(
            op_name=op,
            backend_name=backend,
            estimated_cents=cents,
            actual_cents=cents,
            tokens_in=tin,
            tokens_out=tout,
            duration_seconds=0.1,
            namespace=api_engine.config.namespace,
            ts=now - timedelta(minutes=off),
        )


# ─────────────────────────────────────────────────────────────────
# /cost/summary
# ─────────────────────────────────────────────────────────────────


def test_cost_summary_group_by_op(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    _seed_rows(api_engine)
    r = client.get("/cost/summary?group_by=op", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_by"] == "op"
    keys = {row["key"]: row for row in body["rows"]}
    assert set(keys) == {"audio.transcribe", "text.summarize", "frames.analyze"}
    assert keys["text.summarize"]["count"] == 2
    assert keys["text.summarize"]["total_cents"] == pytest.approx(19.5)
    assert keys["text.summarize"]["total_usd"] == pytest.approx(0.195, rel=1e-3)
    assert keys["audio.transcribe"]["total_cents"] == 0.0
    assert body["total_cents"] == pytest.approx(22.5)


def test_cost_summary_group_by_backend(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    _seed_rows(api_engine)
    r = client.get("/cost/summary?group_by=backend", headers=auth)
    assert r.status_code == 200
    keys = {row["key"]: row for row in r.json()["rows"]}
    assert set(keys) == {"mlx-whisper", "openai", "anthropic", "gemini"}
    assert keys["mlx-whisper"]["count"] == 2


def test_cost_summary_group_by_namespace(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    _seed_rows(api_engine)
    r = client.get("/cost/summary?group_by=namespace", headers=auth)
    assert r.status_code == 200
    keys = {row["key"] for row in r.json()["rows"]}
    assert keys == {api_engine.config.namespace}


def test_cost_summary_empty_ledger(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/cost/summary", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert body["total_cents"] == 0.0


def test_cost_summary_bad_since_400(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/cost/summary?since=not-a-date", headers=auth)
    assert r.status_code == 400


def test_cost_summary_bad_group_by_422(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/cost/summary?group_by=invalid", headers=auth)
    assert r.status_code == 422


def test_cost_summary_since_until_window(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    _seed_rows(api_engine)
    now = datetime.now(UTC)
    # Window cropped to the last ~1.5 minutes — should pick up only
    # the frames.analyze (0 min ago) + text.summarize/anthropic (1 min ago).
    since = (now - timedelta(minutes=1, seconds=30)).isoformat()
    until = now.isoformat()
    r = client.get(
        "/cost/summary",
        params={"since": since, "until": until, "group_by": "op"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    keys = {row["key"] for row in r.json()["rows"]}
    assert keys == {"frames.analyze", "text.summarize"}


def test_cost_summary_requires_token(client: TestClient) -> None:
    r = client.get("/cost/summary")
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────
# /cost/log
# ─────────────────────────────────────────────────────────────────


def test_cost_log_returns_newest_first(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    _seed_rows(api_engine)
    r = client.get("/cost/log", headers=auth)
    assert r.status_code == 200
    body = r.json()
    items = body["items"]
    assert len(items) == 5
    # Newest-first ordering by ts.
    ts_values = [item["ts"] for item in items]
    assert ts_values == sorted(ts_values, reverse=True)
    # Each item has the engine schema we promised.
    sample = items[0]
    for field in (
        "id", "ts", "op_name", "backend_name", "namespace",
        "estimated_cents", "actual_cents", "tokens_in", "tokens_out",
        "duration_seconds",
    ):
        assert field in sample, sample


def test_cost_log_offset_limit_pagination(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    _seed_rows(api_engine)
    r1 = client.get("/cost/log?limit=2&offset=0", headers=auth)
    r2 = client.get("/cost/log?limit=2&offset=2", headers=auth)
    r3 = client.get("/cost/log?limit=2&offset=4", headers=auth)
    assert r1.status_code == r2.status_code == r3.status_code == 200
    page1 = r1.json()
    page2 = r2.json()
    page3 = r3.json()
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert len(page3["items"]) == 1
    assert page1["next_offset"] == 2
    assert page2["next_offset"] == 4
    assert page3["next_offset"] is None
    # No overlap between pages.
    seen = {item["id"] for item in page1["items"]}
    for item in page2["items"]:
        assert item["id"] not in seen
        seen.add(item["id"])


def test_cost_log_filter_by_op(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    _seed_rows(api_engine)
    r = client.get("/cost/log?op=text.summarize", headers=auth)
    assert r.status_code == 200
    items = r.json()["items"]
    assert items
    assert all(item["op_name"] == "text.summarize" for item in items)


def test_cost_log_bad_until_400(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/cost/log?until=not-a-date", headers=auth)
    assert r.status_code == 400


def test_cost_log_limit_upper_bound_422(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/cost/log?limit=999999", headers=auth)
    assert r.status_code == 422


def test_cost_log_requires_token(client: TestClient) -> None:
    r = client.get("/cost/log")
    assert r.status_code == 401
