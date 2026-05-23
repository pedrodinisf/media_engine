"""``pymupdf`` backend for ``document.parse``.

MuPDF bindings (``import fitz``) — page-by-page text extraction. The
import is lazy + inside the call path so this module is import-clean
and registered even when the optional ``document`` extra isn't
installed (the dep is only needed at ``execute()`` time).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Document,
    Kind,
    compute_artifact_id,
    compute_derived_artifact_id,
)
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.document.parse import DocParseParams

BACKEND_NAME = "pymupdf"
BACKEND_VERSION = "1.0.0"


def _import_fitz() -> Any:
    try:
        import fitz  # type: ignore  # noqa: PGH003
    except ImportError as e:
        raise RuntimeError(
            "pymupdf is not installed. Install with: "
            "uv sync --extra document"
        ) from e
    return fitz


def _extract_pages(fitz: Any, path: str) -> tuple[list[dict[str, Any]], str | None]:
    """Open the PDF and return (per-page records, title-from-metadata)."""
    pages: list[dict[str, Any]] = []
    title: str | None = None
    doc: Any = fitz.open(path)
    try:
        meta: dict[str, Any] = dict(getattr(doc, "metadata", None) or {})
        raw_title = meta.get("title")
        if isinstance(raw_title, str) and raw_title.strip():
            title = raw_title
        for page_index in range(int(doc.page_count)):
            page: Any = doc.load_page(page_index)
            text = str(page.get_text("text") or "")
            pages.append({"page_index": page_index, "text": text.rstrip()})
    finally:
        doc.close()
    return pages, title


@register_backend
class PyMuPdfBackend(Backend):
    op_name = "document.parse"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(services=["pymupdf"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, DocParseParams)
        fitz = _import_fitz()

        src = params.source_path
        source_sha = compute_artifact_id(src)
        pages, pdf_title = _extract_pages(fitz, str(src))
        full_text = "\n\n".join(p["text"] for p in pages if p["text"])

        derived_id = compute_derived_artifact_id(
            kind=Kind.Document,
            op_name="document.parse",
            op_version="1.0.0",
            backend_name=BACKEND_NAME,
            backend_version=BACKEND_VERSION,
            params={"mode": params.mode, "source_sha": source_sha},
            input_ids=[],
        )
        payload: dict[str, Any] = {
            "text": full_text,
            "pages": pages,
            "page_count": len(pages),
            "title": pdf_title,
            "source_format": "pdf",
            "source_sha": source_sha,
            "mode": params.mode,
        }
        tmp = ctx.workdir / f"document-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)

        return [
            Document(
                id=derived_id,
                path=dest,
                metadata=payload,
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, DocParseParams)
        try:
            size_mb = params.source_path.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        return CostEstimate(local_seconds=max(0.05, size_mb / 30.0))


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "PyMuPdfBackend"]
