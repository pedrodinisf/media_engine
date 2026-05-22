"""Phase 6 commit 49 — plugin catalog + storage REST surface.

Five endpoints used by the Web UI's Settings panel:

  GET  /plugins/extras    — pyproject extras + install status
  GET  /plugins/catalog   — current op + backend visibility state
  PUT  /plugins/catalog   — overwrite the state (persists to plugins.toml)
  GET  /storage/stats     — bytes-by-kind + free space
  POST /storage/gc        — workdir sweep + LRU eviction (preview by default)

All are bearer-gated. The catalog gate is **enforcement-only**: hidden
entries stay registered, REST + MCP + UI just filter them out. See
``runtime/plugins.py`` for the persistence model.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from media_engine.api._state import AppState
from media_engine.api.routes import get_state, require_token
from media_engine.artifacts import Kind
from media_engine.backends import BackendRegistry
from media_engine.ops import OpRegistry
from media_engine.runtime.cache import ApiTokenInfo, CachedArtifact
from media_engine.runtime.disk_guard import free_gb
from media_engine.runtime.eviction import EvictionPolicy, evict_lru
from media_engine.runtime.gc import sweep_workdirs
from media_engine.runtime.plugins import (
    CatalogState,
    load_catalog,
    save_catalog,
)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Extras catalog — read-only "show, don't install"
# ─────────────────────────────────────────────────────────────────


class ExtraRow(BaseModel):
    name: str
    packages: list[str]
    installed: bool
    install_command: str


class ExtrasResponse(BaseModel):
    items: list[ExtraRow]


# Map of extra name → (pip dist name(s), leaf module to probe). The
# leaf module is what ``importlib.util.find_spec`` looks for; missing
# spec → not installed. We hard-code the map (rather than parsing
# pyproject.toml at request time) so the route is fast + doesn't
# depend on the on-disk pyproject file being installed alongside the
# wheel.
_EXTRAS_CATALOG: list[tuple[str, list[str], str]] = [
    # (extra name, pip dist names, leaf module to probe)
    ("transcribe-mlx", ["mlx-whisper>=0.4"], "mlx_whisper"),
    ("diarize", ["pyannote.audio>=3.1", "torch>=2.2"], "pyannote.audio"),
    ("embed", ["sentence-transformers>=2.7"], "sentence_transformers"),
    ("chunk", ["nltk>=3.8"], "nltk"),
    ("vlm-cloud", ["google-genai>=0.4", "anthropic>=0.40"], "google.genai"),
    ("vlm-local", ["openai>=1.30"], "openai"),
    ("llm-mlx", ["mlx-lm>=0.18"], "mlx_lm"),
    ("ocr", ["rapidocr-onnxruntime>=1.3"], "rapidocr_onnxruntime"),
    ("classify", ["open_clip_torch>=2.24"], "open_clip"),
    ("acquire-url", ["yt-dlp>=2024.5", "playwright>=1.44"], "yt_dlp"),
    ("live", ["pynput>=1.7"], "pynput"),
    ("document", ["pymupdf>=1.24"], "fitz"),
    ("search", ["sqlite-vss>=0.1.2"], "sqlite_vss"),
    ("api", ["fastapi>=0.110", "uvicorn[standard]>=0.30", "sse-starlette>=2.1"], "fastapi"),
    ("mcp", ["mcp>=0.3"], "mcp"),
    ("postgres", ["psycopg[binary]>=3.1", "pgvector>=0.2"], "psycopg"),
]


def _is_installed(leaf_module: str) -> bool:
    try:
        return importlib.util.find_spec(leaf_module) is not None
    except (ValueError, ImportError):
        return False


@router.get("/plugins/extras", response_model=ExtrasResponse)
def get_plugins_extras(
    _state: Annotated[AppState, Depends(get_state)],
    _token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> ExtrasResponse:
    """List the pyproject extras the Web UI's plugin catalog surfaces.

    Each row's ``installed`` field is freshly probed via
    ``importlib.util.find_spec`` on every call — cheap (a dict lookup
    plus a stat on a sys.path entry) and avoids a stale cache after
    the operator runs ``uv sync --extra X`` in another shell.

    The Web UI displays ``install_command`` next to a "copy" button.
    There is no POST endpoint for installation: auto-running
    ``uv sync`` inside the live web process risks corrupting the
    running venv and can take 10+ minutes for ``torch``/``pyannote``;
    plan §13 risk #3 documents the tradeoff + the v1.x hardening path
    (out-of-process installer daemon).
    """
    items = [
        ExtraRow(
            name=name,
            packages=packages,
            installed=_is_installed(leaf),
            install_command=f"uv sync --extra {name}",
        )
        for name, packages, leaf in _EXTRAS_CATALOG
    ]
    return ExtrasResponse(items=items)


# ─────────────────────────────────────────────────────────────────
# Catalog gate — per-op + per-backend visibility
# ─────────────────────────────────────────────────────────────────


class CatalogResponse(BaseModel):
    """Visibility state + the universe of toggleable entries.

    ``ops`` is every registered op name; ``backends`` is every
    ``op.name__backend.name`` key. ``hidden_ops`` / ``hidden_backends``
    are the currently-hidden subsets (from ``plugins.toml``). The Web
    UI renders one checkbox per entry and POSTs back the inverted
    state via PUT /plugins/catalog.
    """

    ops: list[str]
    backends: list[str]
    hidden_ops: list[str]
    hidden_backends: list[str]


class CatalogUpdate(BaseModel):
    hidden_ops: list[str] = Field(default_factory=lambda: cast(list[str], []))
    hidden_backends: list[str] = Field(
        default_factory=lambda: cast(list[str], [])
    )


def _all_backend_keys() -> list[str]:
    """Every registered backend, as ``op.name__backend.name`` keys."""
    keys: list[str] = []
    for backend in BackendRegistry.list_all():
        keys.append(CatalogState.backend_key(backend.op_name, backend.name))
    return sorted(set(keys))


def _catalog_response(state: AppState) -> CatalogResponse:
    """Shared body for GET + PUT — universe + current hidden state."""
    catalog = load_catalog(state.engine.config.config_dir)
    return CatalogResponse(
        ops=sorted(op.name for op in OpRegistry.list_all()),
        backends=_all_backend_keys(),
        hidden_ops=sorted(catalog.hidden_ops),
        hidden_backends=sorted(catalog.hidden_backends),
    )


@router.get("/plugins/catalog", response_model=CatalogResponse)
def get_plugins_catalog(
    state: Annotated[AppState, Depends(get_state)],
    _token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> CatalogResponse:
    return _catalog_response(state)


@router.put("/plugins/catalog", response_model=CatalogResponse)
def put_plugins_catalog(
    body: CatalogUpdate,
    state: Annotated[AppState, Depends(get_state)],
    _token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> CatalogResponse:
    """Replace the catalog gate. Unknown op / backend keys are ignored
    silently — the operator might be hiding entries from an extra
    they're about to install, and writing those keys would surprise
    them with a 400. The runtime filter just doesn't fire on unknown
    keys."""
    next_state = CatalogState(
        hidden_ops=frozenset(body.hidden_ops),
        hidden_backends=frozenset(body.hidden_backends),
    )
    save_catalog(state.engine.config.config_dir, next_state)
    # Echo the new state alongside the (recomputed) universe so the UI
    # gets one round-trip per save.
    return _catalog_response(state)


# ─────────────────────────────────────────────────────────────────
# Storage — stats + GC
# ─────────────────────────────────────────────────────────────────


class StorageStats(BaseModel):
    permanent_store: str
    workdir: str
    namespace: str
    total_bytes: int
    free_gb: float
    by_kind: dict[str, dict[str, int]]


@router.get("/storage/stats", response_model=StorageStats)
def get_storage_stats(
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> StorageStats:
    """Bytes-by-kind + free space rollup.

    Mirrors what ``med storage stats --json`` prints, scoped to the
    token's namespace. Walks ``cached_artifacts`` once + stat-checks
    every file (the same pattern the CLI uses); for a million-row
    cache this is seconds-not-milliseconds, so the UI surfaces a
    "Refresh" button rather than polling on a timer.

    Reuses ``state.engine.cache`` (a long-lived SQLAlchemy session
    factory) — opening a fresh ``Cache(...)`` per request would burn
    a connection-pool spin-up on every Settings tab activation.
    """
    cfg = state.engine.config
    per_kind: dict[str, dict[str, int]] = {
        k.value: {"count": 0, "bytes": 0} for k in Kind
    }
    total = 0
    with state.engine.cache.session() as s:
        for row in s.scalars(
            select(CachedArtifact).where(
                CachedArtifact.namespace == token.namespace
            )
        ).all():
            try:
                size = Path(row.path).stat().st_size
            except OSError:
                size = 0
            total += size
            slot = per_kind.setdefault(
                row.kind, {"count": 0, "bytes": 0}
            )
            slot["count"] += 1
            slot["bytes"] += size
    return StorageStats(
        permanent_store=str(cfg.permanent_store),
        workdir=str(cfg.workdir),
        namespace=token.namespace,
        total_bytes=total,
        free_gb=free_gb(cfg.permanent_store),
        by_kind=per_kind,
    )


class GCRequest(BaseModel):
    """``apply=False`` is the default — preview what *would* happen."""

    apply: bool = False
    sweep_workdirs: bool = True
    evict: bool = True


class GCResponse(BaseModel):
    applied: bool
    workdirs_swept: int
    workdir_candidates: list[str]
    eviction_enabled: bool
    bytes_before: int = 0
    bytes_after: int = 0
    evicted_artifact_ids: list[str] = Field(
        default_factory=lambda: cast(list[str], [])
    )


@router.post("/storage/gc", response_model=GCResponse)
def post_storage_gc(
    body: GCRequest,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> GCResponse:
    """Workdir sweep + (optional) LRU eviction.

    Mirror of ``med storage gc``. When ``apply=False`` the route
    reports what would happen without writing — workdir candidates are
    listed but not deleted, and eviction runs in ``dry_run=True``
    mode. ``eviction_enabled=False`` in engine config disables the
    eviction pass regardless of the ``evict`` flag (the CLI honours
    the same gate).
    """
    cfg = state.engine.config
    retention = timedelta(hours=cfg.gc_workdir_retention_hours)

    workdirs_swept = 0
    workdir_candidates: list[str] = []
    if body.sweep_workdirs:
        if body.apply:
            removed = sweep_workdirs(cfg.workdir, retention=retention)
            workdirs_swept = len(removed)
            workdir_candidates = [str(p) for p in removed]
        else:
            cutoff = (datetime.now(UTC) - retention).timestamp()
            if cfg.workdir.exists():
                for entry in cfg.workdir.iterdir():
                    if not entry.is_dir():
                        continue
                    try:
                        mtime = entry.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if mtime <= cutoff:
                        workdir_candidates.append(str(entry))

    bytes_before = 0
    bytes_after = 0
    evicted_ids: list[str] = []
    eviction_enabled = cfg.eviction_enabled and body.evict
    if eviction_enabled:
        try:
            protected = tuple(
                Kind(k.lower()) for k in cfg.eviction_protected_kinds
            )
        except ValueError as e:
            raise HTTPException(
                status_code=500,
                detail=f"bad eviction_protected_kinds: {e}",
            ) from None
        policy = EvictionPolicy(
            enabled=True,
            max_gb=cfg.eviction_max_gb,
            protected_kinds=protected,
        )
        # Reuse the engine's long-lived cache (Phase 6 post-49 audit) —
        # opening a fresh Cache(...) per GC click cost a connection-pool
        # spin-up on every Settings → Storage button press.
        result = evict_lru(
            state.engine.cache,
            policy,
            namespace=token.namespace,
            dry_run=not body.apply,
        )
        bytes_before = result.bytes_before
        bytes_after = result.bytes_after
        evicted_ids = list(result.evicted_ids)

    return GCResponse(
        applied=body.apply,
        workdirs_swept=workdirs_swept,
        workdir_candidates=workdir_candidates,
        eviction_enabled=eviction_enabled,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        evicted_artifact_ids=evicted_ids,
    )


