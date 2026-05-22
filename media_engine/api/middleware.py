"""Security headers for the Phase 6 Web UI mount.

The `/ui/*` prefix serves the SvelteKit SPA via FastAPI ``StaticFiles``.
Modern browsers honor a stack of opt-in security headers that limit
what the UI page can load and how downstream resources can embed it.
We scope every header to `/ui/*` responses so the API surface (under
the same FastAPI app) is unaffected — REST clients live on
`/operations`, `/jobs`, etc., and don't need a CSP.

Header rationale (commit 40 §9, refined by commit 50):

- ``Content-Security-Policy``
  - ``default-src 'self'`` — only same-origin loads.
  - ``img-src 'self' data: blob:`` — supports inline thumbnails + blob
    previews of artifacts the catalog browser renders (commit 44).
  - ``media-src 'self' blob:`` — same, for ``<video>`` + ``<audio>``.
  - ``worker-src 'self' blob:`` — pdf.js spawns its renderer in a worker
    backed by a blob URL.
  - ``script-src 'self' 'wasm-unsafe-eval' 'unsafe-inline'`` — pdf.js's
    wasm-backed fallback build needs the wasm CSP token; ``'unsafe-inline'``
    is required because SvelteKit's adapter-static bundle emits an inline
    boot ``<script>`` block in index.html that bootstraps the SPA
    (``__sveltekit_<hash> = { base, assets }; Promise.all([…import…])``).
    Without it the SPA never hydrates and the page stays blank. Adapter-
    static can emit a hash-mode meta CSP per build but the hash changes
    every rebuild, and SvelteKit's HTTP-CSP mode requires runtime nonces
    that the static mount can't supply. The tradeoff is acceptable for a
    loopback-first, same-origin UI (no third-party scripts, token already
    XSS-readable in localStorage). A v1.x hardening path (httpOnly cookie
    + hash-rotation build) is catalogued in ``web_ui_deferred.md``.
  - ``style-src 'self' 'unsafe-inline'`` — Svelte's scoped styles + the
    Tailwind v4 runtime inject inline ``<style>`` tags.
- ``X-Content-Type-Options: nosniff`` — refuses to MIME-sniff a binary
  download as HTML/JS.
- ``Referrer-Policy: same-origin`` — minimizes Referer leakage; relevant
  to the ?token= SSE query-param caveat documented in plan §13.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

UI_PREFIX = "/ui"

_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "worker-src 'self' blob:; "
    "script-src 'self' 'wasm-unsafe-eval' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class UISecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds CSP + ancillary security headers to ``/ui/*`` responses.

    Idempotent — never overwrites a header an upstream handler already
    set, so a future per-route override would win. Skipped for any
    request whose path doesn't start with the UI prefix.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if not _is_ui_path(request.url.path):
            return response
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


def _is_ui_path(path: str) -> bool:
    """Strict prefix match: ``/ui`` exact or ``/ui/...``.

    A naive ``startswith("/ui")`` would also match ``/uix`` or
    ``/uixyz``, which would smear UI CSP across unrelated future
    routes. Guarding here is cheaper than relying on every future
    contributor remembering the gotcha.
    """
    return path == UI_PREFIX or path.startswith(UI_PREFIX + "/")
