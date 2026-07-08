"""Phase-7 privacy controls: per-namespace purge, REST export gate, MCP hiding.

Voice fingerprints are biometric, so storage + export are opt-in. These tests
lock those defaults.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_engine.api.app import build_app
from media_engine.api.auth import create_token
from media_engine.artifacts import SpeakerEmbedding
from media_engine.backends import _speaker_store as store
from media_engine.backends._vec import l2_normalize
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine

# ── per-namespace purge ──────────────────────────────────────────────


def _embedding(ns: str, art_id: str, tmp_path: Path) -> SpeakerEmbedding:
    p = tmp_path / f"{art_id[:8]}.json"
    p.write_text("{}")
    return SpeakerEmbedding(
        id=art_id, path=p, namespace=ns,
        metadata={"turns": [], "model": "m", "dimensions": 0},
        created_at=datetime.now(UTC),
    )


def test_purge_namespace_isolates(engine: Engine, tmp_path: Path) -> None:
    alice_art = _embedding("alice", "a" * 64, tmp_path)
    engine.cache.upsert_artifact(alice_art)
    engine.cache.upsert_artifact(_embedding("bob", "b" * 64, tmp_path))
    conn = store.connect(engine.config.permanent_store)
    for ns, sid in [("alice", "Speaker_a"), ("bob", "Speaker_b")]:
        store.upsert_profile(conn, store.StoredProfile(
            speaker_id=sid, namespace=ns, model="m",
            centroid=l2_normalize([1.0, 0.0]), member_count=1, label=None,
        ))
    conn.close()

    assert alice_art.path.exists()  # the raw-vector sidecar is on disk

    result = engine.cache.purge_namespace(
        "alice", permanent_store=engine.config.permanent_store
    )
    assert result["artifacts"] == 1
    assert result["speaker_profiles"] == 1

    # bob's data survives.
    assert engine.cache.get_artifact("b" * 64, namespace="bob") is not None
    assert engine.cache.get_artifact("a" * 64, namespace="alice") is None
    # Privacy: the biometric blob file must be gone too, not just the row.
    assert not alice_art.path.exists()
    conn = store.connect(engine.config.permanent_store)
    assert store.list_profiles(conn, "alice") == []
    assert len(store.list_profiles(conn, "bob")) == 1
    conn.close()


# ── MCP default-hidden invariant ─────────────────────────────────────


def test_speakers_ops_not_in_mcp_default_allowlist() -> None:
    from media_engine.mcp.server import DEFAULT_ALLOWED_OPS

    for op in ("speakers.embed_voice", "speakers.cluster", "speakers.match"):
        assert op not in DEFAULT_ALLOWED_OPS, (
            f"{op} must not be MCP-exposed by default (biometric)"
        )


# ── REST export gate ─────────────────────────────────────────────────


def _engine(tmp_path: Path, *, export: bool) -> Iterator[Engine]:
    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
        speaker_export_enabled=export,
    )
    with Engine.open_quick(cfg) as e:
        yield e


@pytest.fixture
def client_export_off(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, str]]]:
    for e in _engine(tmp_path, export=False):
        auth = {"Authorization": f"Bearer {create_token(e.cache, label='t').secret}"}
        with TestClient(build_app(engine=e)) as c:
            yield c, auth


@pytest.fixture
def client_export_on(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, str]]]:
    for e in _engine(tmp_path, export=True):
        auth = {"Authorization": f"Bearer {create_token(e.cache, label='t').secret}"}
        with TestClient(build_app(engine=e)) as c:
            yield c, auth


def test_rest_run_speakers_blocked_by_default(client_export_off) -> None:
    client, auth = client_export_off
    r = client.post(
        "/run",
        headers=auth,
        json={"op": "speakers.match", "inputs": ["x" * 64], "params": {}},
    )
    assert r.status_code == 403
    assert "biometric" in r.json()["detail"].lower()


def test_rest_run_speakers_identify_not_gated(client_export_off) -> None:
    # The Phase-5 name-based op is not biometric — it stays reachable.
    client, auth = client_export_off
    r = client.post(
        "/run",
        headers=auth,
        json={"op": "speakers.identify", "inputs": ["x" * 64],
              "params": {"speaker_db": "/nonexistent.csv"}},
    )
    # Not a 403 (may 202 accept then fail async, or 400) — just not gated.
    assert r.status_code != 403


def test_rest_run_speakers_allowed_when_enabled(client_export_on) -> None:
    client, auth = client_export_on
    r = client.post(
        "/run",
        headers=auth,
        json={"op": "speakers.match", "inputs": ["x" * 64], "params": {}},
    )
    assert r.status_code == 202  # accepted for submission


_SPEAKER_PIPELINE_YAML = """\
profile_schema_version: "1.0"
name: sneaky
kind: pipeline
description: smuggle a gated op through a pipeline DAG
inputs:
  - { name: source, kind: audio }
graph:
  - id: diar
    op: audio.diarize
    inputs: { audio: source }
  - id: emb
    op: speakers.embed_voice
    inputs: { audio: source, diarization: diar }
outputs: [emb]
"""


def test_rest_pipeline_cannot_bypass_speaker_gate(client_export_off) -> None:
    # A pipeline DAG containing a gated speakers.* op must be refused too —
    # otherwise /pipelines is a hole around the /run gate.
    client, auth = client_export_off
    r = client.post(
        "/pipelines",
        headers=auth,
        json={"pipeline_yaml": _SPEAKER_PIPELINE_YAML,
              "sources": [{"name": "source", "artifact_id": "x" * 64}]},
    )
    assert r.status_code == 403
    assert "speakers.embed_voice" in r.json()["detail"]


def test_rest_pipeline_speaker_allowed_when_enabled(client_export_on) -> None:
    client, auth = client_export_on
    r = client.post(
        "/pipelines",
        headers=auth,
        json={"pipeline_yaml": _SPEAKER_PIPELINE_YAML,
              "sources": [{"name": "source", "artifact_id": "x" * 64}]},
    )
    # Not gated → passes the privacy check (may 404 on the missing source
    # artifact, but must not be a 403 from the speaker gate).
    assert r.status_code != 403
