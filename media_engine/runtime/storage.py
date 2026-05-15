"""Storage backend protocol + LocalFSStorage implementation.

The ``StorageBackend`` protocol is the seam where Phase 5+ optional backends
(S3 etc.) plug in. ``LocalFSStorage`` is the only impl that ships in v1.

Layout:
  {permanent_store}/artifacts/{sha[:2]}/{sha}{ext}   sharded by 2-char prefix
  {workdir}/{job_id}/                                ephemeral per-job tmp
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal, Protocol

LinkMode = Literal["copy", "hardlink"]


class StorageBackend(Protocol):
    """Filesystem-shaped persistence for content-addressed artifacts."""

    def artifact_path(self, sha256: str, ext: str) -> Path: ...
    def store_file(
        self, src: Path, sha256: str, ext: str, link_mode: LinkMode = "copy"
    ) -> Path: ...
    def ensure_workdir(self, job_id: str) -> Path: ...
    def cleanup_workdir(self, job_id: str) -> None: ...


class LocalFSStorage:
    """Default storage: a permanent store + an ephemeral workdir on local FS."""

    def __init__(self, permanent_store: Path, workdir: Path) -> None:
        self.permanent_store = permanent_store
        self.workdir = workdir
        (self.permanent_store / "artifacts").mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)

    def artifact_path(self, sha256: str, ext: str) -> Path:
        if not sha256:
            raise ValueError("sha256 must be non-empty")
        if len(sha256) < 2:
            raise ValueError("sha256 must be at least 2 chars for sharding")
        normalized_ext = ext if not ext or ext.startswith(".") else f".{ext}"
        return self.permanent_store / "artifacts" / sha256[:2] / f"{sha256}{normalized_ext}"

    def store_file(
        self, src: Path, sha256: str, ext: str, link_mode: LinkMode = "copy"
    ) -> Path:
        """Atomically place ``src`` into the content-addressed store.

        Idempotent: if dest exists, returns its path without re-writing.
        Atomicity: write to ``.tmp`` on the same filesystem, then ``os.replace``.
        """
        dest = self.artifact_path(sha256, ext)
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            if tmp.exists():
                tmp.unlink()
            if link_mode == "hardlink":
                os.link(src, tmp)
            else:
                shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        return dest

    def ensure_workdir(self, job_id: str) -> Path:
        d = self.workdir / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cleanup_workdir(self, job_id: str) -> None:
        d = self.workdir / job_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
