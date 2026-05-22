"""Tests for ops/metadata/scrape_page.py.

Always-run layer monkeypatches the ``_scrape`` playwright seam with a
canned dict; a gated smoke runs real Chromium against a stdlib HTTP
server serving a synthetic event page.
"""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from media_engine.artifacts import Analysis, Kind
from media_engine.ops import OperationContext
from media_engine.ops.metadata import scrape_page as sp
from media_engine.ops.metadata.scrape_page import (
    MetadataScrapePage,
    ScrapePageParams,
)
from media_engine.runtime.engine import Engine

PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None

_CANNED = {
    "url": "https://example.com/session",
    "title": "Sample Conference 2026 — A Test Session",
    "session_title": "A Test Session",
    "event_name": "Sample Conference Annual Meeting",
    "description": "A synthetic session for tests.",
    "date": "2026-01-20",
    "time": "18:30–19:45 CET",
    "location": "Sampleville, Testland",
    "speakers": ["Ada Lovelace", "Alan Turing"],
    "speaker_details": [
        {"name": "Ada Lovelace", "title": "Mathematician", "photo_url": None},
        {"name": "Alan Turing", "title": "Computer Scientist", "photo_url": None},
    ],
    "topics": ["Computing"],
    "tags": ["ai", "history"],
}


def test_op_class_attributes() -> None:
    assert MetadataScrapePage.name == "metadata.scrape_page"
    assert MetadataScrapePage.input_kinds == ()
    assert MetadataScrapePage.output_kinds == (Kind.Analysis,)
    assert MetadataScrapePage.default_backend is None


def test_cost_estimate_is_positive() -> None:
    est = MetadataScrapePage().cost_estimate([], ScrapePageParams(url="x"))
    assert est.local_seconds > 0


async def test_scrape_produces_analysis(
    engine: Engine, op_ctx: OperationContext, mocker
) -> None:
    mocker.patch.object(sp, "_scrape", return_value=dict(_CANNED))
    [a] = await MetadataScrapePage().run(
        [], ScrapePageParams(url="https://example.com/session"), op_ctx
    )
    assert isinstance(a, Analysis)
    assert a.kind is Kind.Analysis
    assert a.path.exists()
    assert a.data["title"] == "Sample Conference 2026 — A Test Session"
    assert a.data["speakers"] == ["Ada Lovelace", "Alan Turing"]
    assert a.metadata["url"] == "https://example.com/session"


async def test_scrape_rejects_inputs(
    engine: Engine, op_ctx: OperationContext, sample_mp4, mocker
) -> None:
    from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams

    mocker.patch.object(sp, "_scrape", return_value=dict(_CANNED))
    [v] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), op_ctx
    )
    with pytest.raises(ValueError, match="takes no inputs"):
        await MetadataScrapePage().run(
            [v], ScrapePageParams(url="https://x"), op_ctx
        )


async def test_scrape_cache_hit_on_rerun(
    engine: Engine, mocker
) -> None:
    spy = mocker.patch.object(sp, "_scrape", return_value=dict(_CANNED))
    [a1] = await engine.run("metadata.scrape_page", url="https://example.com/s")
    [a2] = await engine.run("metadata.scrape_page", url="https://example.com/s")
    assert a1.id == a2.id
    assert spy.call_count == 1  # second call served from cache


async def test_scrape_param_change_yields_new_id(
    engine: Engine, mocker
) -> None:
    mocker.patch.object(sp, "_scrape", return_value=dict(_CANNED))
    [a] = await engine.run("metadata.scrape_page", url="https://example.com/a")
    [b] = await engine.run("metadata.scrape_page", url="https://example.com/b")
    assert a.id != b.id


# ─────────────────────────────────────────────────────────────────
# Gated real smoke
# ─────────────────────────────────────────────────────────────────

_HTML = """<!doctype html><html><head>
<title>Sample Conference 2026 — A Test Session</title>
<meta name="description" content="A synthetic session for tests.">
<meta name="keywords" content="ai, history">
</head><body><main>
<h1>A Test Session</h1>
<time datetime="2026-01-20">20 January 2026</time>
<div class="location">Sampleville, Testland</div>
</main></body></html>"""


@pytest.fixture
def html_server() -> Iterator[str]:
    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_HTML.encode())

        def log_message(self, *_a: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_H))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        httpd.shutdown()


@pytest.mark.needs_playwright
async def test_scrape_real_against_local_html(
    engine: Engine, html_server: str
) -> None:
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed")
    try:
        [a] = await engine.run("metadata.scrape_page", url=html_server)
    except RuntimeError as e:
        pytest.skip(f"playwright smoke unavailable: {e}")
    assert isinstance(a, Analysis)
    assert a.data["session_title"] == "A Test Session"
    assert a.data["date"] == "2026-01-20"
