"""``document.parse`` — extract text + structure from a document file.

Backends:

* ``pymupdf`` (default) — fast page-by-page text extraction via the
  MuPDF bindings. Handles PDFs the engine is most likely to encounter
  (papers, slides, transcripts).
* ``unstructured`` (charter §3, *deferred*) — richer element-level
  parsing (tables, headings, lists). Not in this commit; lands when a
  profile actually consumes structured Document.metadata. Charter
  declares both; the engine ships the one with proven demand first.

Identity = derived id over ``{mode, source_sha}`` — path-stable
across machines. Engine cache row keys on the literal ``source_path``
(same caveat as ``acquire.url`` / ``transcript.parse``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)

OP_NAME = "document.parse"
OP_VERSION = "1.0.0"

DocParseMode = Literal["text", "structured"]


class DocParseParams(BaseModel):
    source_path: Path
    mode: DocParseMode = "text"


@register_op
class DocumentParse(Operation):
    """Extract text (and optionally structure) from a document file."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = ()
    output_kinds = (Kind.Document,)
    params_model = DocParseParams
    default_backend = "pymupdf"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, DocParseParams)
        if inputs:
            raise ValueError(
                f"document.parse takes no inputs, "
                f"got {[a.kind for a in inputs]}"
            )
        if not params.source_path.exists():
            raise FileNotFoundError(params.source_path)
        backend_name = ctx.backend or self.default_backend
        if backend_name is None:
            raise RuntimeError(
                f"{self.name} has no backend; pass `backend=` to Engine.run."
            )
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute([], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, DocParseParams)
        try:
            size_mb = params.source_path.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        # pymupdf extraction is fast — roughly 30 MB/s on modern CPUs.
        return CostEstimate(local_seconds=max(0.05, size_mb / 30.0))
