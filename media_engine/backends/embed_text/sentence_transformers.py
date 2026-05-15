"""``sentence-transformers`` backend for ``embed.text``.

Lazy-loaded model (cached in ``ctx.model_pool`` for warm reuse).
``batch_embed`` produces one vector per chunk (or per parent artifact when
the input isn't a Chunks). Sync library wrapped in ``asyncio.to_thread``.

Optional dep: install via ``uv sync --extra embed``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Chunks,
    MarkdownArtifact,
    Transcript,
)
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.embed.text import (
    EmbedTextParams,
    build_embedding_artifact,
)

BACKEND_NAME = "sentence-transformers"
BACKEND_VERSION = "1.0.0"


def _import_st() -> Any:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]  # noqa: I001
    except ImportError as e:
        raise RuntimeError(
            "sentence-transformers is not installed. "
            "Install with: uv sync --extra embed"
        ) from e
    return SentenceTransformer  # type: ignore[no-any-return]


def _load_model_sync(model_id: str) -> Any:
    SentenceTransformer = _import_st()
    return SentenceTransformer(model_id)


def _texts_for(source: AnyArtifact) -> list[tuple[str, int | None]]:
    """Return ``[(text, chunk_index_or_None), ...]`` for embedding."""
    if isinstance(source, Chunks):
        items: list[tuple[str, int | None]] = []
        for chunk in source.chunks:
            text = str(chunk.get("text", ""))
            index = chunk.get("chunk_index")
            items.append((text, int(index) if index is not None else None))
        return items
    if isinstance(source, Transcript):
        text = str(source.metadata.get("text", ""))
        if not text:
            segments = source.metadata.get("segments", [])
            text = " ".join(str(s.get("text", "")).strip() for s in segments).strip()
        return [(text, None)]
    if isinstance(source, MarkdownArtifact):
        return [(str(source.metadata.get("body", "")), None)]
    raise TypeError(f"embed.text does not support kind {source.kind}")


def _embed_batch_sync(
    model: Any, texts: list[str], normalize: bool
) -> list[list[float]]:
    vectors: Any = model.encode(
        texts,
        normalize_embeddings=normalize,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    out: list[list[float]] = []
    for v in vectors:
        out.append([float(x) for x in v.tolist()])
    return out


@register_backend
class SentenceTransformersEmbedTextBackend(Backend):
    op_name = "embed.text"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(hardware=["apple_silicon"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, EmbedTextParams)
        source = inputs[0]
        items = _texts_for(source)
        if not items:
            return []

        cache_key = f"st:{params.model}"
        if ctx.model_pool is not None:
            model = await asyncio.to_thread(
                ctx.model_pool.get_or_load,
                cache_key,
                lambda: _load_model_sync(params.model),
            )
        else:
            model = await asyncio.to_thread(_load_model_sync, params.model)

        texts = [t for t, _ in items]
        vectors = await asyncio.to_thread(
            _embed_batch_sync, model, texts, params.normalize
        )
        return [
            build_embedding_artifact(
                source=source,
                params=params,
                backend_name=BACKEND_NAME,
                backend_version=BACKEND_VERSION,
                workdir_path=ctx.workdir,
                storage=ctx.storage,
                vector=vec,
                chunk_text=text,
                chunk_index=idx,
            )
            for (text, idx), vec in zip(items, vectors, strict=True)
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        source = inputs[0]
        if isinstance(source, Chunks):
            return CostEstimate(local_seconds=0.001 * len(source.chunks) + 1.0)
        return CostEstimate(local_seconds=1.0)


__all__ = [
    "BACKEND_NAME",
    "BACKEND_VERSION",
    "SentenceTransformersEmbedTextBackend",
]
