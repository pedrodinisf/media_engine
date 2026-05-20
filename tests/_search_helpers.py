"""Synthetic-artifact builders for the ``search.*`` tests.

The search backends index whatever's in the engine cache. To keep the
ranking tests deterministic and dep-free we build small in-memory
artifacts directly, persist their JSON sidecars through the engine's
storage, and upsert them into the cache. No ffmpeg / no ML model
needed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from media_engine.artifacts import (
    Document,
    Embedding,
    Kind,
    Transcript,
    WebPage,
    compute_derived_artifact_id,
)
from media_engine.runtime.engine import Engine


def _store_json(engine: Engine, payload: dict[str, Any], key: str) -> tuple[str, Any]:
    """Persist ``payload`` under a derived id keyed by ``key``."""
    derived_id = compute_derived_artifact_id(
        kind=Kind.Transcript,  # placeholder — not part of the stored row's identity
        op_name="_test.search.synth",
        op_version="1",
        backend_name=None,
        backend_version=None,
        params={"key": key},
        input_ids=[],
    )
    workdir = engine.storage.ensure_workdir(f"synth-{key[:20]}")
    tmp = workdir / f"a-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload))
    dest = engine.storage.store_file(tmp, derived_id, ".json")
    return derived_id, dest


def make_transcript(engine: Engine, *, key: str, text: str) -> Transcript:
    payload = {
        "text": text,
        "segments": [
            {"start": 0.0, "end": 1.0, "speaker_id": "A", "text": text}
        ],
        "language": "en",
        "model": "test",
    }
    art_id, dest = _store_json(engine, payload, key=f"transcript-{key}")
    art = Transcript(
        id=art_id, path=dest, metadata=payload, created_at=datetime.now(UTC)
    )
    engine.cache.upsert_artifact(art)
    return art


def make_document(engine: Engine, *, key: str, text: str, title: str = "Doc") -> Document:
    payload = {
        "text": text,
        "pages": [{"page_index": 0, "text": text}],
        "page_count": 1,
        "title": title,
        "source_format": "pdf",
        "source_sha": "deadbeef" * 8,
    }
    art_id, dest = _store_json(engine, payload, key=f"document-{key}")
    art = Document(
        id=art_id, path=dest, metadata=payload, created_at=datetime.now(UTC)
    )
    engine.cache.upsert_artifact(art)
    return art


def make_webpage(engine: Engine, *, key: str, url: str, text: str) -> WebPage:
    payload = {
        "url": url, "title": f"Page {key}", "text": text,
        "status_code": 200, "content_type": "text/html",
        "render_js": False, "html": f"<p>{text}</p>",
    }
    art_id, dest = _store_json(engine, payload, key=f"webpage-{key}")
    art = WebPage(
        id=art_id, path=dest, metadata=payload, created_at=datetime.now(UTC)
    )
    engine.cache.upsert_artifact(art)
    return art


def make_embedding(
    engine: Engine,
    *,
    key: str,
    vector: list[float],
    source: Transcript | Document | WebPage | None = None,
) -> Embedding:
    payload: dict[str, Any] = {"vector": vector, "model": "test-fake-mini"}
    derived_id = compute_derived_artifact_id(
        kind=Kind.Embedding,
        op_name="_test.search.synth",
        op_version="1",
        backend_name="test-fake",
        backend_version="1",
        params={"key": f"embedding-{key}"},
        input_ids=[source.id] if source is not None else [],
    )
    workdir = engine.storage.ensure_workdir(f"emb-{key[:20]}")
    tmp = workdir / f"e-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload))
    dest = engine.storage.store_file(tmp, derived_id, ".json")
    art = Embedding(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(source.id,) if source is not None else (),
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(art)
    return art
