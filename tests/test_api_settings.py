"""REST coverage for /settings/* — doctor, secrets, config-files."""

from __future__ import annotations

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_engine.api.app import build_app
from media_engine.api.auth import create_token
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine
from media_engine.runtime.secrets import KNOWN_SECRETS, read_secrets, secrets_path


@pytest.fixture
def api_engine(tmp_path: Path) -> Iterator[Engine]:
    # Tests in this module exercise PUT /settings/secrets, which calls
    # load_secrets(override=True) and writes directly into os.environ
    # bypassing pytest's monkeypatch tracking. Snapshot the keys it
    # could touch and restore on teardown so a write in one test
    # doesn't leak a fake GEMINI_API_KEY into the rest of the suite —
    # which would silently mask later real-API smoke tests with
    # `API_KEY_INVALID` from Google.
    import os
    SECRET_KEYS = (
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "MEDIA_ENGINE_FULLTEXT_DB_URL",
        "MEDIA_ENGINE_SEMANTIC_DB_URL",
    )
    env_snapshot = {k: os.environ.get(k) for k in SECRET_KEYS}

    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
    )
    try:
        with Engine.open_quick(cfg) as e:
            yield e
    finally:
        for k, v in env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def client(api_engine: Engine) -> Iterator[TestClient]:
    app = build_app(engine=api_engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(api_engine: Engine) -> dict[str, str]:
    secret = create_token(api_engine.cache, label="test").secret
    return {"Authorization": f"Bearer {secret}"}


# ─────────────────────────────────────────────────────────────────
# /settings/doctor
# ─────────────────────────────────────────────────────────────────


def test_doctor_returns_summary_and_ops(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/settings/doctor", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "summary" in body
    assert {"ok", "degraded", "unavailable"} <= set(body["summary"])
    assert isinstance(body["ops"], list) and len(body["ops"]) >= 10
    # Every op row has the contract fields.
    for op in body["ops"]:
        assert {"op_name", "backends", "overall", "embedded"} <= set(op)


def test_doctor_filter_by_op(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get("/settings/doctor", headers=auth, params={"op": "search."})
    assert r.status_code == 200
    names = [o["op_name"] for o in r.json()["ops"]]
    assert names, "search.* prefix should match registered ops"
    assert all(n.startswith("search.") for n in names)


def test_doctor_requires_auth(client: TestClient) -> None:
    r = client.get("/settings/doctor")
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────
# /settings/secrets
# ─────────────────────────────────────────────────────────────────


def test_secrets_list_known_catalog(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/settings/secrets", headers=auth)
    assert r.status_code == 200
    body = r.json()
    names = {row["name"] for row in body["items"]}
    catalog_names = {e["name"] for e in KNOWN_SECRETS}
    assert names == catalog_names
    # No value field is ever returned.
    for row in body["items"]:
        assert "value" not in row
        assert {
            "name",
            "label",
            "set",
            "source",
            "unblocks_direct",
            "unblocks_indirect",
            "adds_alternate",
        } <= set(row)


def test_postgres_env_vars_match_backends(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Catalog ships MEDIA_ENGINE_FULLTEXT_DB_URL + ..._SEMANTIC_DB_URL,
    matching what the postgres-tsvector / pgvector backends actually read
    (search: media_engine/backends/search/*). The earlier catalog
    listed MEDIA_ENGINE_DATABASE_URL which doesn't exist anywhere —
    setting it via the UI would have done nothing."""
    r = client.get("/settings/secrets", headers=auth)
    names = {row["name"] for row in r.json()["items"]}
    assert "MEDIA_ENGINE_FULLTEXT_DB_URL" in names
    assert "MEDIA_ENGINE_SEMANTIC_DB_URL" in names
    assert "MEDIA_ENGINE_DATABASE_URL" not in names


def _clear_known_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a deterministic baseline: all known secret env-vars unset.

    Without this, the test's view of `unblocks_direct` shifts based on
    the dev's local shell (e.g. having ANTHROPIC_API_KEY set means
    intelligence.extract is already satisfied via claude, which moves
    GEMINI's unblock of the same op out of the `direct` bucket).
    """
    for name in (
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "MEDIA_ENGINE_FULLTEXT_DB_URL",
        "MEDIA_ENGINE_SEMANTIC_DB_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_secrets_impact_gemini_unblocks_intelligence_composites(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no LLM env var is set, GEMINI_API_KEY unblocks
    intelligence.extract (direct) and the three intelligence.*
    composites that delegate to it (indirect). We assert on the
    intelligence chain because the vision ops (image.classify,
    image.ocr) have local alternative backends that may or may not
    be importable on the test runner."""
    _clear_known_secrets(monkeypatch)
    r = client.get("/settings/secrets", headers=auth)
    gemini = next(
        row for row in r.json()["items"] if row["name"] == "GEMINI_API_KEY"
    )
    # Either direct (no local mlx-lm) or alternate (mlx-lm available),
    # but intelligence.extract must appear in SOME impact bucket.
    intel_buckets = (
        gemini["unblocks_direct"]
        + gemini["unblocks_indirect"]
        + gemini["adds_alternate"]
    )
    assert "intelligence.extract" in intel_buckets


@pytest.mark.needs_pyannote
def test_secrets_impact_hf_unblocks_diarize(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF_TOKEN unblocks audio.diarize (pyannote's only *env* requirement —
    the backend also hardware-gates on apple_silicon, so this only holds
    on that platform)."""
    _clear_known_secrets(monkeypatch)
    r = client.get("/settings/secrets", headers=auth)
    hf = next(row for row in r.json()["items"] if row["name"] == "HF_TOKEN")
    assert "audio.diarize" in hf["unblocks_direct"]


def test_secrets_put_writes_file_with_0600(
    client: TestClient,
    auth: dict[str, str],
    api_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear env so the file is the only source.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    r = client.put(
        "/settings/secrets",
        headers=auth,
        json={"updates": {"GEMINI_API_KEY": "test-key-value"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "GEMINI_API_KEY" in body["written"]

    p = secrets_path(api_engine.config.config_dir)
    assert p.exists()
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert read_secrets(api_engine.config.config_dir) == {
        "GEMINI_API_KEY": "test-key-value"
    }

    # Subsequent GET reflects "set" status, sourced from the file.
    r2 = client.get("/settings/secrets", headers=auth)
    gemini = next(
        row for row in r2.json()["items"] if row["name"] == "GEMINI_API_KEY"
    )
    assert gemini["set"] is True
    assert gemini["source"] == "file"


def test_secrets_put_deletes_via_none(
    client: TestClient,
    auth: dict[str, str],
    api_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    client.put(
        "/settings/secrets",
        headers=auth,
        json={"updates": {"HF_TOKEN": "abc"}},
    )
    assert read_secrets(api_engine.config.config_dir) == {"HF_TOKEN": "abc"}
    client.put(
        "/settings/secrets",
        headers=auth,
        json={"updates": {"HF_TOKEN": None}},
    )
    assert read_secrets(api_engine.config.config_dir) == {}


def test_secrets_put_rejects_bad_key(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.put(
        "/settings/secrets",
        headers=auth,
        json={"updates": {"bad-key": "x"}},
    )
    assert r.status_code == 422


# ─────────────────────────────────────────────────────────────────
# /settings/config-files
# ─────────────────────────────────────────────────────────────────


def test_config_files_returns_all_three(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/settings/config-files", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert {"config_toml", "resources_yaml", "secrets_env"} == set(body)
    for view in body.values():
        assert {"path", "exists", "content", "is_masked"} <= set(view)


def test_config_files_masks_secret_values(
    client: TestClient,
    auth: dict[str, str],
    api_engine: Engine,
) -> None:
    # Seed a secrets.env so the viewer has something to mask.
    config_dir = api_engine.config.config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    secrets_path(config_dir).write_text("GEMINI_API_KEY=should-not-leak\n")

    r = client.get("/settings/config-files", headers=auth)
    secrets_view = r.json()["secrets_env"]
    assert secrets_view["exists"] is True
    assert secrets_view["is_masked"] is True
    assert "should-not-leak" not in secrets_view["content"]
    assert "GEMINI_API_KEY=<set>" in secrets_view["content"]


def test_config_files_reports_missing(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/settings/config-files", headers=auth)
    body = r.json()
    # tmp_path config dir is fresh, so neither file exists by default.
    assert body["config_toml"]["exists"] is False
    assert body["resources_yaml"]["exists"] is False
    assert body["config_toml"]["content"] == ""
