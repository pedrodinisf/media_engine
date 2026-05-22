"""Engine configuration.

Effective settings = defaults ← config.toml (``~/.config/media_engine/config.toml``)
← environment variables (``MEDIA_ENGINE_*``). Validation deliberately defers
storage-volume checks to ``validate_storage()`` so the config object can be
constructed cheaply (e.g. for ``med config``).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_config_dir() -> Path:
    return Path.home() / ".config" / "media_engine"


def _default_permanent_store() -> Path:
    return Path("/Volumes/UNIVERSE_V/MEDIA/media_engine")


def _default_workdir() -> Path:
    return Path("/tmp/media_engine")


class EngineConfig(BaseSettings):
    """Effective engine config."""

    model_config = SettingsConfigDict(
        env_prefix="MEDIA_ENGINE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    permanent_store: Path = Field(default_factory=_default_permanent_store)
    workdir: Path = Field(default_factory=_default_workdir)
    config_dir: Path = Field(default_factory=_default_config_dir)
    # Accept both ``MEDIA_ENGINE_CACHE_DB_URL`` (the canonical field name) and
    # ``MEDIA_ENGINE_DB_URL`` (the shorter alias the plan + IaaC docs use).
    cache_db_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "MEDIA_ENGINE_CACHE_DB_URL", "MEDIA_ENGINE_DB_URL", "cache_db_url"
        ),
    )
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    log_format: Literal["text", "json"] = "text"
    log_level: str = "INFO"
    min_free_gb: int = 20
    daemon_socket: Path | None = None
    namespace: str = "default"
    # LRU eviction (Phase 4 commit 32) — opt-in cap on artifact store
    # size. Honored by ``med storage gc --apply`` and the daemon's
    # periodic GC. The default-off policy means existing installs keep
    # current behavior; flip ``eviction_enabled = true`` in config.toml
    # to activate.
    eviction_enabled: bool = False
    eviction_max_gb: float = 500.0
    # Comma- or list-form; Pydantic accepts either.
    eviction_protected_kinds: tuple[str, ...] = ("video", "audio")
    # Garbage-collect orphan workdirs older than this many hours
    # (also used by ``med storage gc --workdirs``).
    gc_workdir_retention_hours: int = 24
    # Phase 6: cap on a single ``POST /acquire/upload`` body to prevent
    # a malicious client from filling the disk through the new web-UI
    # multipart endpoint. The upload streams to a tmp file and aborts
    # past this limit; the rest of the engine (acquire.upload op, daemon
    # client, CLI ingest) is unaffected.
    max_upload_mb: int = 2048

    @classmethod
    def load(cls, config_file: Path | None = None) -> EngineConfig:
        """Load config from a TOML file (default ``~/.config/media_engine/config.toml``),
        then merge environment variables on top."""
        toml_data: dict[str, Any] = {}
        if config_file is None:
            default_toml = _default_config_dir() / "config.toml"
            if default_toml.exists():
                config_file = default_toml
        if config_file is not None and config_file.exists():
            with config_file.open("rb") as f:
                toml_data = tomllib.load(f)
        return cls(**toml_data)

    def resolve_cache_db_url(self) -> str:
        if self.cache_db_url is not None:
            return self.cache_db_url
        return f"sqlite+pysqlite:///{self.permanent_store / 'cache.db'}"

    def validate_storage(self) -> None:
        """Ensure ``permanent_store`` exists and is writable."""
        store = self.permanent_store
        if not store.exists():
            try:
                store.mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError) as e:
                raise RuntimeError(
                    f"permanent_store does not exist and cannot be created: {store}\n"
                    f"Reason: {e}\n"
                    f"Suggestion: set MEDIA_ENGINE_PERMANENT_STORE to a writable path "
                    f"(e.g. ~/.local/share/media_engine), or mount the default volume "
                    f"({_default_permanent_store()})."
                ) from e
        if not os.access(store, os.W_OK):
            raise RuntimeError(
                f"permanent_store exists but is not writable: {store}\n"
                f"Suggestion: chmod, or set MEDIA_ENGINE_PERMANENT_STORE elsewhere."
            )
