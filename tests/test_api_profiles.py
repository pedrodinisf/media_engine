"""Phase 6 commit 47 — profile workspace REST surface.

``POST /profiles/validate`` returns 200 with ``ok={True,False}``; the
Web UI's live-compile indicator polls it on every YAML edit.
``DELETE /profiles/{name}`` removes user-overrideable profiles; bundled
profiles are read-only.
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


# ─────────────────────────────────────────────────────────────────
# /profiles/validate
# ─────────────────────────────────────────────────────────────────


VALID_PIPELINE = """
name: test-pipeline
kind: pipeline
inputs:
  - { name: source, kind: video }
graph:
  - { id: audio,      op: video.extract_audio,       inputs: { in: source } }
  - { id: transcript, op: audio.transcribe,          inputs: { audio: audio } }
outputs: [transcript]
""".strip()


def test_validate_happy_path(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/profiles/validate",
        json={"pipeline_yaml": VALID_PIPELINE},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["error_class"] is None
    assert {n["id"] for n in body["compiled_nodes"]} == {"audio", "transcript"}
    audio = next(n for n in body["compiled_nodes"] if n["id"] == "audio")
    assert audio["op"] == "video.extract_audio"
    assert audio["inputs"] == ["source"]


def test_validate_unknown_op_returns_compile_error(
    client: TestClient, auth: dict[str, str]
) -> None:
    yaml = """
name: bad-pipeline
kind: pipeline
graph:
  - { id: x, op: no.such.op }
""".strip()
    r = client.post("/profiles/validate", json={"pipeline_yaml": yaml}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "ProfileCompileError"
    assert "no.such.op" in body["message"]


def test_validate_cycle_returns_compile_error(
    client: TestClient, auth: dict[str, str]
) -> None:
    """``validate_and_sort`` raises CycleError / ValueError for cycles;
    the route folds both into a ProfileCompileError envelope."""
    yaml = """
name: cycle-pipeline
kind: pipeline
inputs:
  - { name: source, kind: video }
graph:
  - { id: a, op: video.extract_audio, inputs: { in: b } }
  - { id: b, op: video.extract_audio, inputs: { in: a } }
""".strip()
    r = client.post("/profiles/validate", json={"pipeline_yaml": yaml}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "ProfileCompileError"


def test_validate_malformed_yaml_returns_load_error(
    client: TestClient, auth: dict[str, str]
) -> None:
    yaml = "name: test\nkind: pipeline\ngraph: [unclosed"
    r = client.post("/profiles/validate", json={"pipeline_yaml": yaml}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "ProfileLoadError"
    # PyYAML attaches a problem_mark; we expose it as 1-based line.
    # The unclosed bracket is on line 3 → some integer reported.
    assert body["line"] is None or isinstance(body["line"], int)


def test_validate_missing_required_field_returns_load_error(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Pydantic validation failure inside the loader is reported as a
    ProfileLoadError envelope."""
    yaml = """
name: nograph
kind: pipeline
""".strip()
    r = client.post("/profiles/validate", json={"pipeline_yaml": yaml}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "ProfileLoadError"


def test_validate_prompt_profile_happy(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Prompt profiles are validated structurally (default_op exists)."""
    yaml = """
name: my-prompt
kind: prompt
default_op: video.multimodal
body: |
  Summarize this video.
""".strip()
    r = client.post("/profiles/validate", json={"pipeline_yaml": yaml}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    (node,) = body["compiled_nodes"]
    # Structural fields (unchanged contract).
    assert node["id"] == "run"
    assert node["op"] == "video.multimodal"
    assert node["backend"] is None
    assert node["inputs"] == []
    # Phase 8 enrichment — video.multimodal defaults to a gemini (cloud) model.
    assert node["resolved_backend"] == "gemini"
    assert node["provider"] == "cloud"
    assert any(
        m["name"] == "model" and m["provider"] == "cloud" for m in node["models"]
    )


def test_validate_requires_token(client: TestClient) -> None:
    r = client.post("/profiles/validate", json={"pipeline_yaml": "name: x"})
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────
# DELETE /profiles/{name}
# ─────────────────────────────────────────────────────────────────


def _seed_user_profile(api_engine: Engine, name: str, content: str) -> Path:
    profiles_dir = api_engine.config.config_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{name}.yaml"
    path.write_text(content)
    return path


def test_delete_user_profile(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    path = _seed_user_profile(api_engine, "doomed", VALID_PIPELINE)
    assert path.exists()
    r = client.delete("/profiles/doomed", headers=auth)
    assert r.status_code == 204, r.text
    assert not path.exists()


def test_delete_missing_profile_404(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.delete("/profiles/never-existed", headers=auth)
    assert r.status_code == 404


def test_delete_refuses_bundled_profile(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Bundled profiles live in `<repo>/profiles/`, not the user's
    config dir. Deleting one shouldn't be possible from the API even
    if the name is known."""
    r = client.delete("/profiles/analysis-full", headers=auth)
    # The user dir doesn't contain analysis-full (the bundled one
    # lives in the repo dir, which we never touch here) → 404.
    assert r.status_code == 404


def test_delete_invalid_name_400(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Kebab-regex enforcement on the name segment."""
    # Path-traversal segments get normalised by the URL router and
    # come back as 404 (no matching route); for in-segment invalid
    # characters (uppercase) the handler's regex check fires with 400.
    r = client.delete("/profiles/UPPERCASE-Not-Kebab", headers=auth)
    assert r.status_code == 400
    assert "invalid profile name" in r.text


def test_delete_requires_token(client: TestClient) -> None:
    r = client.delete("/profiles/anything")
    assert r.status_code == 401


def test_delete_only_touches_user_dir_not_bundled(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    """DELETE removes the file from `{config_dir}/profiles/` only.
    A same-named bundled profile in `<repo>/profiles/` is never touched
    because the resolver scopes itself to the user dir.
    """
    name = "analysis-full"
    user_copy = _seed_user_profile(api_engine, name, VALID_PIPELINE)
    assert user_copy.exists()

    # Delete removes the user copy.
    r = client.delete(f"/profiles/{name}", headers=auth)
    assert r.status_code == 204
    assert not user_copy.exists()

    # The bundled `analysis-full` still resolves through discovery
    # (we never touched <repo>/profiles/).
    r = client.get("/profiles", headers=auth)
    by_name = {p["name"]: p for p in r.json()}
    assert name in by_name
    assert not by_name[name]["path"].endswith(
        f"config/profiles/{name}.yaml"
    )


# ─────────────────────────────────────────────────────────────────
# ProfileSummary.source field (post-commit-48 audit)
# ─────────────────────────────────────────────────────────────────


def test_summary_source_field_marks_user_profiles(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    """Server stamps `source` per row so the Web UI doesn't need a
    `/config/`-substring heuristic to tell bundled from user."""
    # Seed a user profile whose YAML `name:` matches the filename so
    # the discovered key is what we expect.
    yaml = VALID_PIPELINE.replace(
        "name: test-pipeline", "name: my-user-profile"
    )
    _seed_user_profile(api_engine, "my-user-profile", yaml)
    r = client.get("/profiles", headers=auth)
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()}
    assert "my-user-profile" in by_name
    assert by_name["my-user-profile"]["source"] == "user"


def test_summary_source_field_marks_bundled_profiles(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Bundled profiles (shipped in `<repo>/profiles/`) report
    `source: bundled` so the UI hides destructive controls."""
    r = client.get("/profiles", headers=auth)
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()}
    assert "analysis-full" in by_name
    assert by_name["analysis-full"]["source"] == "bundled"


def test_post_profile_returns_source_user(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A profile written via POST /profiles always lands in the user
    dir and the returned summary reflects that."""
    r = client.post(
        "/profiles",
        json={
            "profile_schema_version": "1.0",
            "name": "post-source-test",
            "kind": "pipeline",
            "description": "",
            "inputs": [{"name": "source", "kind": "video"}],
            "graph": [
                {"id": "audio", "op": "video.extract_audio", "inputs": {"in": "source"}}
            ],
            "outputs": ["audio"],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    assert r.json()["source"] == "user"


# ─────────────────────────────────────────────────────────────────
# /profiles/validate — string-loader fast path (post-commit-48 audit)
# ─────────────────────────────────────────────────────────────────


def test_validate_does_not_touch_disk_workdir(
    client: TestClient, auth: dict[str, str], api_engine: Engine
) -> None:
    """Post-commit-48 audit: validate parses YAML in memory and must
    not create + clean a tmp workdir on every call. Asserts the
    workdir tree size is unchanged across validate calls — the
    pre-fix path created + tore down a workdir per request, the
    new path uses load_profile_from_string."""
    workdir_root = api_engine.config.workdir
    before = (
        sorted(workdir_root.iterdir()) if workdir_root.exists() else []
    )
    r = client.post(
        "/profiles/validate",
        json={"pipeline_yaml": VALID_PIPELINE},
        headers=auth,
    )
    assert r.status_code == 200
    after = (
        sorted(workdir_root.iterdir()) if workdir_root.exists() else []
    )
    assert after == before


# ─────────────────────────────────────────────────────────────────
# Phase 8 — validate enrichment · /profiles digest · /pipelines/preview
# ─────────────────────────────────────────────────────────────────


def test_validate_enriches_nodes_with_provider(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.post(
        "/profiles/validate", json={"pipeline_yaml": VALID_PIPELINE}, headers=auth
    )
    body = r.json()
    audio = next(n for n in body["compiled_nodes"] if n["id"] == "audio")
    # video.extract_audio is a local ffmpeg op.
    assert audio["provider"] in ("local", "unknown", "composite")
    # every enriched node carries the additive fields
    for n in body["compiled_nodes"]:
        assert "resolved_backend" in n
        assert "models" in n
        assert "requirement_hint" in n


def test_profiles_list_carries_digest(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/profiles", headers=auth)
    assert r.status_code == 200
    profiles = r.json()
    assert profiles, "bundled profiles should be discovered"
    for p in profiles:
        assert "digest" in p
        assert isinstance(p["digest"]["models"], list)
        assert isinstance(p["digest"]["requirement_hints"], list)


def test_pipeline_preview_compile_error_envelope(
    client: TestClient, auth: dict[str, str]
) -> None:
    # An op typo compiles-fails → 200 with ok=False + typed envelope, mirroring
    # /profiles/validate (so the workspace doesn't special-case the preview).
    bad = VALID_PIPELINE.replace("audio.transcribe", "audio.not_a_real_op")
    r = client.post(
        "/pipelines/preview", json={"pipeline_yaml": bad, "sources": []}, headers=auth
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "ProfileCompileError"


def test_pipeline_preview_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/pipelines/preview", json={"pipeline_yaml": VALID_PIPELINE, "sources": []}
    )
    assert r.status_code == 401
