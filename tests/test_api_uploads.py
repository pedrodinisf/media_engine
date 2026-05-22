"""Phase 6 commit 41 — multipart upload + URL-probe endpoints.

Exercises the additive surface in ``media_engine/api/uploads.py``: the
multipart upload (preview + commit modes), the size limit, the
non-media rejection, and the URL probe's resolvable / not-resolvable
branches.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from media_engine.api.app import build_app
from media_engine.api.auth import create_token
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def api_engine(tmp_path: Path) -> Iterator[Engine]:
    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
        # Tight cap so we can exercise the 413 branch on a small fixture.
        max_upload_mb=1,
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
# POST /acquire/upload — preview mode
# ─────────────────────────────────────────────────────────────────


def test_upload_preview_returns_image_kind(
    client: TestClient, auth: dict[str, str]
) -> None:
    with (FIXTURES / "sample.png").open("rb") as f:
        r = client.post(
            "/acquire/upload",
            files={"file": ("sample.png", f, "image/png")},
            data={"commit": "false"},
            headers=auth,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "image"
    assert body["size_bytes"] > 0
    assert len(body["sha256_prefix"]) == 16


def test_upload_preview_returns_video_kind(
    client: TestClient, auth: dict[str, str]
) -> None:
    with (FIXTURES / "sample.mp4").open("rb") as f:
        r = client.post(
            "/acquire/upload",
            files={"file": ("sample.mp4", f, "video/mp4")},
            data={"commit": "false"},
            headers=auth,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "video"
    assert body["duration_s"] is not None


def test_upload_rejects_non_media(
    client: TestClient, auth: dict[str, str], tmp_path: Path
) -> None:
    junk = tmp_path / "junk.txt"
    junk.write_bytes(b"not a media file" * 16)
    with junk.open("rb") as f:
        r = client.post(
            "/acquire/upload",
            files={"file": ("junk.txt", f, "text/plain")},
            data={"commit": "false"},
            headers=auth,
        )
    assert r.status_code == 400
    assert "probe" in r.text


def test_upload_enforces_size_limit(
    client: TestClient, auth: dict[str, str], tmp_path: Path
) -> None:
    """1 MB cap + a 2 MB blob → 413."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x00" * (2 * 1024 * 1024))
    with big.open("rb") as f:
        r = client.post(
            "/acquire/upload",
            files={"file": ("big.bin", f, "application/octet-stream")},
            data={"commit": "false"},
            headers=auth,
        )
    assert r.status_code == 413
    assert "MEDIA_ENGINE_MAX_UPLOAD_MB" in r.text


def test_upload_requires_token(client: TestClient) -> None:
    with (FIXTURES / "sample.png").open("rb") as f:
        r = client.post(
            "/acquire/upload",
            files={"file": ("sample.png", f, "image/png")},
            data={"commit": "false"},
        )
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────
# POST /acquire/upload — commit mode
# ─────────────────────────────────────────────────────────────────


def test_upload_commit_returns_job_id(
    client: TestClient, auth: dict[str, str]
) -> None:
    with (FIXTURES / "sample.png").open("rb") as f:
        r = client.post(
            "/acquire/upload",
            files={"file": ("sample.png", f, "image/png")},
            data={"commit": "true"},
            headers=auth,
        )
    # 202 Accepted via JobAck — same status the rest of the async surface
    # returns. (FastAPI defaults to 200 for response_model unions; we
    # accept either as long as job_id is present.)
    assert r.status_code in {200, 202}, r.text
    body = r.json()
    assert "job_id" in body
    assert isinstance(body["job_id"], str)


# ─────────────────────────────────────────────────────────────────
# POST /acquire/url/probe
# ─────────────────────────────────────────────────────────────────


def test_url_probe_returns_metadata_when_yt_dlp_succeeds(
    client: TestClient, auth: dict[str, str]
) -> None:
    fake_info = {
        "title": "Test Video",
        "duration": 42.5,
        "uploader": "Test Channel",
        "thumbnail": "https://example.com/thumb.jpg",
        "formats": [{"format_id": "1"}, {"format_id": "2"}],
    }
    with patch(
        "media_engine.api.uploads._yt_dlp_dump", return_value=(fake_info, None)
    ):
        r = client.post(
            "/acquire/url/probe",
            json={"url": "https://example.com/video"},
            headers=auth,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolvable"] is True
    assert body["title"] == "Test Video"
    assert body["duration_s"] == 42.5
    assert body["uploader"] == "Test Channel"
    assert body["formats_available"] == 2


def test_url_probe_reports_unresolvable_when_yt_dlp_fails(
    client: TestClient, auth: dict[str, str]
) -> None:
    with patch(
        "media_engine.api.uploads._yt_dlp_dump",
        return_value=(None, "yt-dlp not installed"),
    ):
        r = client.post(
            "/acquire/url/probe",
            json={"url": "https://example.com/missing"},
            headers=auth,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolvable"] is False
    assert "yt-dlp" in body["reason"]


def test_url_probe_requires_token(client: TestClient) -> None:
    r = client.post(
        "/acquire/url/probe", json={"url": "https://example.com/v"}
    )
    assert r.status_code == 401
