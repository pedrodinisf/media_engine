"""Shared HTML → (title, plaintext) extraction for the web.fetch backends.

Stdlib ``html.parser`` only — no BeautifulSoup dependency. The result is
small and lossy on purpose: title + visible text + scrubbed
script/style. Downstream ops (chunk.semantic, embed.text,
intelligence.*) consume the text; the raw HTML stays on the WebPage's
``metadata['html']`` for callers that want it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from media_engine.artifacts import (
    Kind,
    WebPage,
    compute_derived_artifact_id,
)
from media_engine.ops import OperationContext
from media_engine.ops.web.fetch import OP_NAME, OP_VERSION, WebFetchParams

_SKIP_TAGS = {"script", "style", "noscript", "template"}
_WS_RE = re.compile(r"\s+")


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title = ((self.title or "") + data).strip() or None
            return
        self._chunks.append(data)

    @property
    def text(self) -> str:
        return _WS_RE.sub(" ", " ".join(self._chunks)).strip()


def extract_title_and_text(html: str) -> tuple[str | None, str]:
    """Return ``(title, plaintext)`` extracted from an HTML document."""
    ex = _Extractor()
    try:
        ex.feed(html)
        ex.close()
    except Exception:
        pass
    return ex.title, ex.text


def build_webpage_artifact(
    *,
    params: WebFetchParams,
    backend_name: str,
    backend_version: str,
    html: str,
    status_code: int,
    content_type: str | None,
    ctx: OperationContext,
) -> WebPage:
    """Persist a fetched HTML payload as a typed WebPage artifact.

    Shared between the httpx and playwright backends so the derived id,
    JSON sidecar, and metadata shape are identical regardless of fetch
    path.
    """
    title, text = extract_title_and_text(html)
    derived_id = compute_derived_artifact_id(
        kind=Kind.WebPage,
        op_name=OP_NAME,
        op_version=OP_VERSION,
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[],
    )
    payload: dict[str, Any] = {
        "url": params.url,
        "title": title,
        "text": text,
        "status_code": status_code,
        "content_type": content_type,
        "render_js": params.render_js,
        "html": html,
    }
    tmp = ctx.workdir / f"webpage-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = ctx.storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return WebPage(
        id=derived_id,
        path=dest,
        metadata=payload,
        created_at=datetime.now(UTC),
    )
