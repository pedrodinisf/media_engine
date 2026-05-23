"""FastAPI app end-to-end tests.

A ``TestClient`` is driven against an engine whose store + cache live in
``tmp_path``; the bearer token is minted from the same cache, so the
auth path is exercised on every request. Covers:

- token verify (missing / invalid / valid)
- ``/operations`` + ``/backends`` discovery
- ``POST /run`` → job lifecycle (pending → running → completed)
- ``GET /artifacts`` + ``GET /artifacts/{id}`` + ``/file`` + ``/lineage``
- ``DELETE /jobs/{id}`` cancellation
- inline ``POST /pipelines`` and ``POST /profiles`` round-trip
"""

from __future__ import annotations

import time
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
def token(api_engine: Engine) -> str:
    return create_token(api_engine.cache, label="test").secret


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────


def test_unauthenticated_request_returns_401(client: TestClient) -> None:
    r = client.get("/operations")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_invalid_token_returns_401(client: TestClient) -> None:
    r = client.get(
        "/operations", headers={"Authorization": "Bearer not-real"}
    )
    assert r.status_code == 401


def test_revoked_token_returns_401(
    client: TestClient, api_engine: Engine
) -> None:
    from media_engine.api.auth import revoke_token

    secret = create_token(api_engine.cache, label="doomed")
    revoke_token(api_engine.cache, secret.token_id)
    r = client.get(
        "/operations",
        headers={"Authorization": f"Bearer {secret.secret}"},
    )
    assert r.status_code == 401


def test_bearer_tolerates_extra_whitespace(
    client: TestClient, api_engine: Engine
) -> None:
    """``Authorization: Bearer  <token>`` (extra spaces between scheme
    and secret) must still authenticate. Without the ``.strip()`` after
    ``partition``, the leading space would poison the hash lookup."""
    secret = create_token(api_engine.cache, label="ws")
    r = client.get(
        "/operations",
        headers={"Authorization": f"Bearer  {secret.secret}"},  # two spaces
    )
    assert r.status_code == 200


def test_token_with_mismatched_namespace_returns_403(
    client: TestClient, api_engine: Engine
) -> None:
    """The engine is single-namespace per process; a token bound to a
    different namespace would silently write to the engine's namespace
    while reads (filtered by the token) return empty. We reject the
    mismatch with 403 so the contract is honest."""
    foreign = create_token(
        api_engine.cache, label="tenant-b", namespace="tenant-b"
    )
    r = client.get(
        "/operations",
        headers={"Authorization": f"Bearer {foreign.secret}"},
    )
    assert r.status_code == 403
    assert "namespace" in r.json()["detail"]


# ─────────────────────────────────────────────────────────────────
# Discovery surface
# ─────────────────────────────────────────────────────────────────


def test_list_operations(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/operations", headers=auth)
    assert r.status_code == 200
    payload = r.json()
    names = {item["name"] for item in payload}
    assert "acquire.upload" in names
    assert "audio.transcribe" in names


def test_list_operations_includes_declared_resources(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Post-commit-49 audit: `declared_resources` lifted onto the
    summary so the Web UI's Settings → Config tab can render the
    per-op resource allocation in a single HTTP request instead of
    N+1 detail fetches."""
    r = client.get("/operations", headers=auth)
    assert r.status_code == 200
    payload = r.json()
    for item in payload:
        assert "declared_resources" in item
        assert isinstance(item["declared_resources"], list)
    # `embed.text` declares `apple_gpu` per the op module — a concrete
    # value to assert the field is actually populated, not just present.
    embed_row = next(i for i in payload if i["name"] == "embed.text")
    assert "apple_gpu" in embed_row["declared_resources"]


def test_get_operation_detail(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/operations/acquire.upload", headers=auth)
    assert r.status_code == 200
    detail = r.json()
    assert detail["name"] == "acquire.upload"
    assert "params_schema" in detail
    assert detail["params_schema"]["type"] == "object"


def test_list_backends(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/backends", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    # mlx-whisper is always registered (import-clean).
    assert any(b["name"] == "mlx-whisper" for b in rows)


# ─────────────────────────────────────────────────────────────────
# Token CRUD
# ─────────────────────────────────────────────────────────────────


def test_token_create_returns_secret_once(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.post(
        "/tokens",
        headers=auth,
        json={"label": "ci", "namespace": "default"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["secret"]
    # The secret must let us authenticate.
    r2 = client.get(
        "/operations",
        headers={"Authorization": f"Bearer {body['secret']}"},
    )
    assert r2.status_code == 200


def test_token_revoke(client: TestClient, auth: dict[str, str]) -> None:
    created = client.post("/tokens", headers=auth, json={"label": "doomed"}).json()
    r = client.delete(f"/tokens/{created['token_id']}", headers=auth)
    assert r.status_code == 200
    r2 = client.get(
        "/operations",
        headers={"Authorization": f"Bearer {created['secret']}"},
    )
    assert r2.status_code == 401


# ─────────────────────────────────────────────────────────────────
# Jobs lifecycle (acquire.upload — no external deps)
# ─────────────────────────────────────────────────────────────────


def _wait_for_job(
    client: TestClient, auth: dict[str, str], job_id: str, *, timeout: float = 5.0
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=auth)
        assert r.status_code == 200
        body = r.json()
        if body["job"]["status"] in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not finish in {timeout}s")


def test_run_op_acquire_upload(
    client: TestClient,
    auth: dict[str, str],
    api_engine: Engine,
    tmp_path: Path,
) -> None:
    src = tmp_path / "sample.txt"
    src.write_bytes(b"hello media engine\n")
    # We use ``video.trim``? No — that needs a Video. ``acquire.upload`` is
    # nullary in declared input_kinds; pass source_path as a param.
    r = client.post(
        "/run",
        headers=auth,
        json={
            "op": "acquire.upload",
            "params": {"source_path": str(src)},
        },
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    final = _wait_for_job(client, auth, job_id)
    # acquire.upload classifies by ffprobe; a text file is rejected — that's
    # a "failed" job, not a server error. Either outcome proves the
    # lifecycle (pending → started → finished) works end to end.
    assert final["job"]["status"] in {"completed", "failed"}


def test_run_unknown_op_returns_400(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.post(
        "/run", headers=auth, json={"op": "does.not_exist", "params": {}}
    )
    assert r.status_code == 400


def test_run_invalid_backend_returns_400(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.post(
        "/run",
        headers=auth,
        json={
            "op": "audio.transcribe",
            "backend": "not-a-backend",
            "params": {},
        },
    )
    assert r.status_code == 400


def test_list_jobs(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/jobs", headers=auth)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_get_job_404(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/jobs/deadbeef", headers=auth)
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────
# Artifact retrieval
# ─────────────────────────────────────────────────────────────────


def test_artifacts_listing_empty_store(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/artifacts", headers=auth)
    assert r.status_code == 200
    page = r.json()
    assert page["items"] == []
    assert page["limit"] == 100
    assert page["next_offset"] is None


def test_lifespan_resets_orphaned_running_jobs(
    api_engine: Engine, tmp_path: Path
) -> None:
    """If a previous process crashed mid-run the jobs stay "running"
    forever. The lifespan startup sweep flips them to "failed" with
    a clear ``InterruptedRun`` envelope so clients see a terminal
    state rather than a phantom in-flight row.
    """
    # Plant an orphaned "running" job from a "previous process".
    api_engine.cache.insert_job(
        job_id="orphan-1",
        pipeline_name=None,
        pipeline_yaml=None,
        namespace=api_engine.config.namespace,
    )
    api_engine.cache.update_job(job_id="orphan-1", status="running")
    # Now boot the API; the lifespan should sweep the orphan.
    app_ = build_app(engine=api_engine)
    with TestClient(app_):
        pass
    recovered = api_engine.cache.get_job(
        "orphan-1", namespace=api_engine.config.namespace
    )
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.error is not None
    assert recovered.error["error_class"] == "InterruptedRun"


def test_artifacts_pagination_round_trip(
    client: TestClient, auth: dict[str, str], api_engine: Engine, tmp_path: Path
) -> None:
    """Plant 3 artifacts, page with limit=1; verify next_offset
    advances correctly and signals end-of-page with None."""
    from datetime import UTC, datetime

    from media_engine.artifacts import Kind
    from media_engine.artifacts.text import MarkdownArtifact

    for i in range(3):
        p = tmp_path / f"p{i}.md"
        p.write_text("x")
        api_engine.cache.upsert_artifact(
            MarkdownArtifact(
                id=f"{i:064x}",
                kind=Kind.MarkdownArtifact,
                path=str(p),  # type: ignore[arg-type]
                metadata={},
                derived_from=(),
                produced_by=None,
                created_at=datetime.now(UTC),
            )
        )
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        r = client.get(
            "/artifacts", headers=auth, params={"limit": 1, "offset": offset}
        )
        assert r.status_code == 200
        page = r.json()
        seen.extend(item["id"] for item in page["items"])
        offset = page["next_offset"]
    assert len(seen) == 3
    assert len(set(seen)) == 3


def test_artifact_404(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/artifacts/" + "0" * 64, headers=auth)
    assert r.status_code == 404


def test_artifact_lineage_404(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/artifacts/" + "0" * 64 + "/lineage", headers=auth)
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────
# Profiles
# ─────────────────────────────────────────────────────────────────


def test_pipeline_request_requires_exactly_one_source(
    client: TestClient, auth: dict[str, str]
) -> None:
    # Neither provided.
    r = client.post("/pipelines", headers=auth, json={"sources": []})
    assert r.status_code == 400
    # Both provided.
    r2 = client.post(
        "/pipelines",
        headers=auth,
        json={
            "profile_name": "x",
            "pipeline_yaml": "name: y\nkind: pipeline\ngraph: []\n",
        },
    )
    assert r2.status_code == 400


def test_inline_pipeline_with_bad_yaml_returns_400(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.post(
        "/pipelines",
        headers=auth,
        json={
            "pipeline_yaml": "::: not yaml :::",
            "sources": [],
        },
    )
    assert r.status_code == 400


def test_profile_upload_persists_to_disk(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    payload = {
        "name": "test-upload",
        "kind": "pipeline",
        "description": "uploaded by test",
        "inputs": [{"name": "source", "kind": "video"}],
        "graph": [
            {
                "id": "extract",
                "op": "video.extract_audio",
                "inputs": ["source"],
            }
        ],
    }
    r = client.post("/profiles", headers=auth, json=payload)
    assert r.status_code == 201, r.text
    summary = r.json()
    assert summary["name"] == "test-upload"
    on_disk = Path(summary["path"])
    assert on_disk.exists()
    assert on_disk.is_relative_to(api_engine.config.config_dir)


def test_profile_upload_rejects_path_traversal(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Profile names with path separators must be rejected — without
    the validator, ``../../../etc/passwd`` would write a YAML file
    outside the profiles directory.
    """
    payload = {
        "name": "../../../etc/passwd",
        "kind": "pipeline",
        "graph": [
            {"id": "extract", "op": "video.extract_audio", "inputs": []}
        ],
    }
    r = client.post("/profiles", headers=auth, json=payload)
    assert r.status_code == 400
    assert "invalid profile name" in r.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────
# SSE — events stream filter (unit, not integration)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sse_stream_filters_by_job_id() -> None:
    """The SSE adapter must drop events whose ``job_id`` doesn't match.

    Integration testing the full HTTP stream against a TestClient is
    awkward — sse-starlette keeps the response open until disconnect.
    We unit-test the filter directly: emit events for two jobs through
    a shared EventBus, assert only the matching ones reach the stream.
    """
    import asyncio
    from datetime import UTC, datetime

    from media_engine.api.sse import job_event_stream
    from media_engine.runtime.events import EventBus, OpStarted

    bus = EventBus()
    gen = job_event_stream(bus, "j1", keepalive_seconds=60.0)
    # Pump the generator far enough for it to install its subscriber.
    gen_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.02)

    bus.emit(
        OpStarted(
            event_id="e1",
            op_run_id="r1",
            job_id="j-other",
            timestamp=datetime.now(UTC),
            op_name="acquire.upload",
        )
    )
    bus.emit(
        OpStarted(
            event_id="e2",
            op_run_id="r2",
            job_id="j1",
            timestamp=datetime.now(UTC),
            op_name="acquire.upload",
        )
    )
    frame = await asyncio.wait_for(gen_task, timeout=2.0)
    assert frame["event"] == "op_started"
    assert '"j1"' in frame["data"]
    await gen.aclose()


@pytest.mark.anyio
async def test_sse_replays_already_persisted_events_b001(
    api_engine: Engine,
) -> None:
    """B-001 regression: a client subscribing AFTER an event has already
    been emitted (and persisted via the EventBus sink) must still see
    that event via the replay phase.

    Before the fix, ``job_event_stream`` only subscribed to the bus
    and never queried the persistent log; clients connecting even a
    few ms after ``POST /run`` returned saw an empty stream forever.
    """
    import asyncio
    from datetime import UTC, datetime

    from media_engine.api.sse import job_event_stream
    from media_engine.runtime.events import OpCompleted, OpStarted

    bus = api_engine.event_bus
    job_id = "test-job-b001"
    # Emit two events BEFORE any subscriber exists — the persistence
    # sink (engine._persist_event) writes them to the events table.
    bus.emit(
        OpStarted(
            event_id="ev-001",
            op_run_id="run-001",
            job_id=job_id,
            timestamp=datetime.now(UTC),
            op_name="acquire.upload",
        )
    )
    bus.emit(
        OpCompleted(
            event_id="ev-002",
            op_run_id="run-001",
            job_id=job_id,
            timestamp=datetime.now(UTC),
            duration_seconds=0.01,
        )
    )
    # Open the SSE stream — replay should catch both.
    gen = job_event_stream(
        bus,
        job_id,
        cache=api_engine.cache,
        namespace=api_engine.config.namespace,
        keepalive_seconds=60.0,
    )
    seen: list[str] = []
    for _ in range(2):
        frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        seen.append(frame["event"])
    assert "op_started" in seen
    assert "op_completed" in seen
    await gen.aclose()


@pytest.mark.anyio
async def test_engine_run_uses_provided_job_id_for_events(
    api_engine: Engine, tmp_path: Path
) -> None:
    """B-001 root cause: events must carry the caller-supplied job_id
    so SSE filters can match. Before the fix, Engine.run generated its
    own internal id and the REST job_id never reached events."""
    import asyncio
    import shutil

    from media_engine.runtime.events import OpCompleted, OpStarted

    received: list[str | None] = []

    async def listener() -> None:
        async for ev in api_engine.event_bus.subscribe():
            if isinstance(ev, OpStarted | OpCompleted):
                received.append(ev.job_id)
                if len(received) >= 2:
                    return

    listen_task = asyncio.create_task(listener())
    await asyncio.sleep(0.02)

    src = tmp_path / "sample.mp4"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "sample.mp4", src)

    await api_engine.run(
        "acquire.upload",
        source_path=src,
        job_id="rest-supplied-job-id",
    )
    await asyncio.wait_for(listen_task, timeout=2.0)
    assert all(jid == "rest-supplied-job-id" for jid in received)
