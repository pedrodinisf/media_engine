"""Tests for ops/web/fetch.py + httpx / playwright backends.

httpx is a core dep → its smoke is always-run against a stdlib HTTP
server. playwright is optional → its smoke is gated.
"""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from media_engine.artifacts import Kind, WebPage
from media_engine.backends import BackendRegistry
from media_engine.ops.web.fetch import OP_NAME, WebFetch, WebFetchParams
from media_engine.runtime.engine import Engine

PLAYWRIGHT = importlib.util.find_spec("playwright") is not None

_HTML = """<!doctype html><html><head>
<title>Test Page</title>
<style>body { color: red; }</style>
</head><body>
<h1>Welcome</h1>
<p>This is the body text.</p>
<script>console.log('do not capture this')</script>
<p>More body text here.</p>
</body></html>"""


# ─────────────────────────────────────────────────────────────────
# Op contract
# ─────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert WebFetch.name == "web.fetch"
    assert WebFetch.input_kinds == ()
    assert WebFetch.output_kinds == (Kind.WebPage,)
    assert WebFetch.default_backend == "httpx"


def test_select_backend_by_render_js() -> None:
    op = WebFetch()
    assert op.select_backend(WebFetchParams(url="x")) is None
    assert (
        op.select_backend(WebFetchParams(url="x", render_js=True)) == "playwright"
    )


def test_cost_estimate_distinguishes_render() -> None:
    op = WebFetch()
    cheap = op.cost_estimate([], WebFetchParams(url="x"))
    pricey = op.cost_estimate([], WebFetchParams(url="x", render_js=True))
    assert pricey.local_seconds > cheap.local_seconds


def test_backends_registered() -> None:
    backends = BackendRegistry.for_op("web.fetch")
    assert "httpx" in backends
    # playwright is import-clean and registered even without playwright.
    assert "playwright" in backends


def test_extract_title_and_text_strips_scripts_and_styles() -> None:
    from media_engine.backends.web._html import extract_title_and_text

    title, text = extract_title_and_text(_HTML)
    assert title == "Test Page"
    assert "Welcome" in text
    assert "This is the body text." in text
    assert "More body text here." in text
    # script/style content scrubbed
    assert "do not capture this" not in text
    assert "color: red" not in text


# ─────────────────────────────────────────────────────────────────
# httpx backend — always-run against a local server
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def html_server() -> Iterator[str]:
    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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


async def test_httpx_fetches_local_page(
    engine: Engine, html_server: str
) -> None:
    [page] = await engine.run(OP_NAME, url=html_server)
    assert isinstance(page, WebPage)
    assert page.kind is Kind.WebPage
    assert page.url == html_server
    assert page.title == "Test Page"
    assert page.metadata["status_code"] == 200
    assert page.metadata["render_js"] is False
    assert "Welcome" in page.metadata["text"]
    assert "do not capture this" not in page.metadata["text"]


async def test_httpx_cache_hit_on_rerun(
    engine: Engine, html_server: str, mocker
) -> None:
    from media_engine.backends.web import httpx as backend_module

    [a] = await engine.run(OP_NAME, url=html_server)
    spy = mocker.spy(backend_module.HttpxWebFetchBackend, "execute")
    [b] = await engine.run(OP_NAME, url=html_server)
    assert spy.call_count == 0  # served from cache
    assert a.id == b.id


async def test_url_change_yields_new_id(
    engine: Engine, html_server: str
) -> None:
    [a] = await engine.run(OP_NAME, url=html_server)
    [b] = await engine.run(OP_NAME, url=html_server + "?v=2")
    assert a.id != b.id


async def test_render_js_dispatches_to_playwright(
    engine: Engine, html_server: str, mocker
) -> None:
    """The op should resolve the playwright backend when render_js=True."""
    from media_engine.backends.web import playwright as pw_backend

    # Stub the playwright fetch so we don't need a real browser.
    mocker.patch.object(
        pw_backend,
        "_fetch_sync",
        return_value=(200, "text/html", _HTML),
    )
    [page] = await engine.run(OP_NAME, url=html_server, render_js=True)
    assert page.metadata["render_js"] is True
    # Different backend → different (cache-key) id than the httpx fetch.
    [static] = await engine.run(OP_NAME, url=html_server)
    assert page.id != static.id


async def test_rejects_inputs(engine: Engine, sample_mp4) -> None:
    from media_engine.ops import OperationContext
    from media_engine.ops.acquire.upload import (
        AcquireUpload,
        AcquireUploadParams,
    )

    workdir = engine.storage.ensure_workdir("t")
    ctx = OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )
    [v] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), ctx
    )
    engine.cache.upsert_artifact(v)
    with pytest.raises(ValueError, match="expects no inputs"):
        await engine.run(OP_NAME, inputs=[v.id], url="https://x")


# ─────────────────────────────────────────────────────────────────
# Real playwright smoke (gated)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.needs_playwright
async def test_playwright_fetches_local_page(
    engine: Engine, html_server: str
) -> None:
    if not PLAYWRIGHT:
        pytest.skip("playwright not installed")
    try:
        [page] = await engine.run(
            OP_NAME, url=html_server, render_js=True
        )
    except RuntimeError as e:
        pytest.skip(f"playwright smoke unavailable: {e}")
    assert isinstance(page, WebPage)
    assert page.title == "Test Page"
    assert "Welcome" in page.metadata["text"]
