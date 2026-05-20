"""Tests for ops/document/parse.py + the pymupdf backend.

Op contract is always-run; the pymupdf real smoke is gated by
``importorskip("fitz")`` against the committed ``tiny.pdf`` fixture.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from media_engine.artifacts import Document, Kind
from media_engine.backends import BackendRegistry
from media_engine.ops import OperationContext
from media_engine.ops.document.parse import (
    OP_NAME,
    DocParseParams,
    DocumentParse,
)
from media_engine.runtime.engine import Engine

PYMUPDF = importlib.util.find_spec("fitz") is not None


def test_op_class_attributes() -> None:
    assert DocumentParse.name == "document.parse"
    assert DocumentParse.input_kinds == ()
    assert DocumentParse.output_kinds == (Kind.Document,)
    assert DocumentParse.default_backend == "pymupdf"


def test_backend_registered() -> None:
    backends = BackendRegistry.for_op("document.parse")
    # pymupdf is import-clean; registered even without the optional dep.
    assert "pymupdf" in backends


def test_cost_estimate_scales_with_size(tmp_path: Path) -> None:
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4\n%empty\n%%EOF\n")
    est = DocumentParse().cost_estimate(
        [], DocParseParams(source_path=f, mode="text")
    )
    assert est.local_seconds > 0


async def test_missing_file_raises(
    op_ctx: OperationContext, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        await DocumentParse().run(
            [],
            DocParseParams(source_path=tmp_path / "no.pdf", mode="text"),
            op_ctx,
        )


async def test_rejects_inputs(
    op_ctx: OperationContext, sample_mp4: Path
) -> None:
    from media_engine.ops.acquire.upload import (
        AcquireUpload,
        AcquireUploadParams,
    )

    [v] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), op_ctx
    )
    with pytest.raises(ValueError, match="takes no inputs"):
        await DocumentParse().run(
            [v],
            DocParseParams(source_path=sample_mp4, mode="text"),
            op_ctx,
        )


# ─────────────────────────────────────────────────────────────────
# Real pymupdf smoke (gated by importorskip + committed tiny.pdf)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.needs_pymupdf
async def test_parse_tiny_pdf(
    op_ctx: OperationContext, tiny_pdf: Path
) -> None:
    pytest.importorskip("fitz")
    [doc] = await DocumentParse().run(
        [], DocParseParams(source_path=tiny_pdf, mode="text"), op_ctx
    )
    assert isinstance(doc, Document)
    assert doc.kind is Kind.Document
    assert doc.path.exists()
    assert doc.page_count == 2
    assert doc.title == "Tiny Test PDF"
    assert "media-engine test document" in doc.metadata["text"]
    assert "Second page line." in doc.metadata["text"]
    assert doc.metadata["source_format"] == "pdf"
    assert doc.metadata["source_sha"]


@pytest.mark.needs_pymupdf
async def test_parse_cache_hit_on_rerun(
    engine: Engine, tiny_pdf: Path, mocker
) -> None:
    pytest.importorskip("fitz")
    from media_engine.backends.document import pymupdf as pm

    [d1] = await engine.run(OP_NAME, source_path=tiny_pdf, mode="text")
    spy = mocker.spy(pm.PyMuPdfBackend, "execute")
    [d2] = await engine.run(OP_NAME, source_path=tiny_pdf, mode="text")
    assert spy.call_count == 0
    assert d1.id == d2.id


@pytest.mark.needs_pymupdf
async def test_mode_change_yields_new_id(
    engine: Engine, tiny_pdf: Path
) -> None:
    pytest.importorskip("fitz")
    [a] = await engine.run(OP_NAME, source_path=tiny_pdf, mode="text")
    [b] = await engine.run(OP_NAME, source_path=tiny_pdf, mode="structured")
    assert a.id != b.id


@pytest.mark.needs_pymupdf
async def test_pages_are_addressable(
    op_ctx: OperationContext, tiny_pdf: Path
) -> None:
    pytest.importorskip("fitz")
    [doc] = await DocumentParse().run(
        [], DocParseParams(source_path=tiny_pdf, mode="text"), op_ctx
    )
    pages = doc.metadata["pages"]
    assert [p["page_index"] for p in pages] == [0, 1]
    assert "media-engine test document" in pages[0]["text"]
    assert "Second page line." in pages[1]["text"]
