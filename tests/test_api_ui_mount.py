"""Phase 6 commit 40 — /ui StaticFiles mount + security headers.

Covers the additive surface in ``media_engine/api/app.py`` and
``media_engine/api/middleware.py``: the mount only attaches when the
dist tree is present, every ``/ui/*`` response carries the security
headers, and the API surface (``/operations``, ``/jobs``, etc.) is
unaffected by the middleware.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from media_engine.api.app import build_app, ui_dist_dir
from media_engine.api.middleware import UI_PREFIX
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
def ui_dist(tmp_path: Path) -> Path:
    """Synthesize a minimal SvelteKit-shaped dist tree."""
    dist = tmp_path / "ui-dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><body><h1>media_engine UI</h1></body></html>"
    )
    asset_dir = dist / "_app"
    asset_dir.mkdir()
    (asset_dir / "fake.js").write_text("/* test asset */")
    return dist


@pytest.fixture
def client_with_ui(api_engine: Engine, ui_dist: Path) -> Iterator[TestClient]:
    """``TestClient`` for an app whose `ui_dist_dir()` resolves to a real tree."""
    with patch("media_engine.api.app.ui_dist_dir", return_value=ui_dist):
        app = build_app(engine=api_engine)
        with TestClient(app) as c:
            yield c


@pytest.fixture
def client_without_ui(api_engine: Engine, tmp_path: Path) -> Iterator[TestClient]:
    missing = tmp_path / "no-ui-here"
    with patch("media_engine.api.app.ui_dist_dir", return_value=missing):
        app = build_app(engine=api_engine)
        with TestClient(app) as c:
            yield c


# ─────────────────────────────────────────────────────────────────
# Mount lifecycle
# ─────────────────────────────────────────────────────────────────


def test_ui_index_html_served_when_dist_present(client_with_ui: TestClient) -> None:
    r = client_with_ui.get("/ui/")
    assert r.status_code == 200
    assert "media_engine UI" in r.text


def test_ui_index_html_at_explicit_path(client_with_ui: TestClient) -> None:
    r = client_with_ui.get("/ui/index.html")
    assert r.status_code == 200
    assert "media_engine UI" in r.text


def test_ui_mount_skipped_when_dist_absent(client_without_ui: TestClient) -> None:
    """No /ui mount when the dist tree is missing — headless deploys are fine."""
    r = client_without_ui.get("/ui/")
    # FastAPI returns 404 for unmounted paths.
    assert r.status_code == 404


def test_api_surface_still_works_alongside_ui(client_with_ui: TestClient) -> None:
    """/operations is still bearer-gated even when /ui is mounted."""
    r = client_with_ui.get("/operations")
    assert r.status_code == 401


def test_health_and_ready_remain_unauthenticated(
    client_with_ui: TestClient,
) -> None:
    """Probes stay accessible regardless of UI mount state."""
    assert client_with_ui.get("/health").status_code == 200
    # /ready may be 200 or 503 depending on tmp_path writability;
    # only the contract that it answers without auth matters here.
    r = client_with_ui.get("/ready")
    assert r.status_code in {200, 503}


# ─────────────────────────────────────────────────────────────────
# Security headers
# ─────────────────────────────────────────────────────────────────


def test_ui_responses_carry_csp_header(client_with_ui: TestClient) -> None:
    r = client_with_ui.get("/ui/")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy")
    assert csp is not None
    # CSP must include the wasm + worker tokens pdf.js needs (plan §9).
    assert "default-src 'self'" in csp
    assert "wasm-unsafe-eval" in csp
    assert "worker-src 'self' blob:" in csp
    assert "frame-ancestors 'none'" in csp


def test_ui_responses_carry_nosniff_and_referrer_policy(
    client_with_ui: TestClient,
) -> None:
    r = client_with_ui.get("/ui/")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "same-origin"


def test_api_responses_do_NOT_carry_ui_csp(client_with_ui: TestClient) -> None:
    """CSP is scoped to /ui/* — API responses stay clean."""
    r = client_with_ui.get("/operations")  # 401, but headers still apply
    assert r.headers.get("content-security-policy") is None


def test_ui_prefix_constant_matches_mount_path() -> None:
    """Guards against UI_PREFIX and the StaticFiles mount drifting apart."""
    assert UI_PREFIX == "/ui"


def test_ui_prefix_match_is_strict() -> None:
    """`/uix` etc. must NOT match — naive startswith would smear CSP."""
    from media_engine.api.middleware import _is_ui_path

    assert _is_ui_path("/ui") is True
    assert _is_ui_path("/ui/") is True
    assert _is_ui_path("/ui/index.html") is True
    assert _is_ui_path("/ui/_app/foo.js") is True
    # Adjacent paths that share the prefix as a string but aren't /ui:
    assert _is_ui_path("/uix") is False
    assert _is_ui_path("/uixyz") is False
    assert _is_ui_path("/uiblob") is False
    # Unrelated paths stay clean.
    assert _is_ui_path("/operations") is False
    assert _is_ui_path("/health") is False
    assert _is_ui_path("/") is False


# ─────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────


def test_ui_dist_dir_resolves_inside_package() -> None:
    """`ui_dist_dir` returns a path inside the package — installable wheels
    must find the bundled SPA without env-var configuration."""
    p = ui_dist_dir()
    assert p.parts[-3:] == ("media_engine", "web", "dist")
