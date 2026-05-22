"""Phase 6 commit 42 — POST /run/preview.

Bearer-gated cost-preview endpoint. Drives ``Engine.estimate_op_cost``
without submitting a job; the UI's run panel debounces this on every
param change.
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
def auth(api_engine: Engine) -> dict[str, str]:
    secret = create_token(api_engine.cache, label="test").secret
    return {"Authorization": f"Bearer {secret}"}


def test_run_preview_returns_cost_shape(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Happy path: known op with valid params returns the cost-summary shape."""
    r = client.post(
        "/run/preview",
        json={
            "op": "audio.transcribe",
            "inputs": [],
            "params": {"model": "mlx-community/whisper-large-v3-mlx"},
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["op"] == "audio.transcribe"
    assert "backend" in body
    assert "estimate_seconds_local" in body
    assert "estimate_cost_cents" in body
    assert "estimate_tokens_in" in body
    assert "estimate_tokens_out" in body


def test_run_preview_resolves_default_backend(
    client: TestClient, auth: dict[str, str]
) -> None:
    """When no `backend=` is passed, the preview reports the resolved default."""
    r = client.post(
        "/run/preview",
        json={"op": "audio.transcribe", "inputs": [], "params": {}},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    # audio.transcribe ships exactly one backend (mlx-whisper) — the
    # preview should pick it up via op.default_backend.
    assert r.json()["backend"] is not None


def test_run_preview_unknown_op_400(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/run/preview",
        json={"op": "no.such.op", "inputs": [], "params": {}},
        headers=auth,
    )
    assert r.status_code == 400
    assert "unknown op" in r.text


def test_run_preview_invalid_params_422(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Pydantic validation surfaces as 422 (not 500)."""
    r = client.post(
        "/run/preview",
        json={
            "op": "audio.transcribe",
            "inputs": [],
            "params": {"temperature": "not-a-number"},
        },
        headers=auth,
    )
    assert r.status_code == 422


def test_run_preview_unresolvable_input_404(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.post(
        "/run/preview",
        json={
            "op": "audio.transcribe",
            "inputs": ["nonexistent-artifact-id"],
            "params": {},
        },
        headers=auth,
    )
    assert r.status_code == 404


def test_run_preview_requires_token(client: TestClient) -> None:
    r = client.post(
        "/run/preview",
        json={"op": "audio.transcribe", "inputs": [], "params": {}},
    )
    assert r.status_code == 401
