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

from pydantic import AliasChoices, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_config_dir() -> Path:
    return Path.home() / ".config" / "media_engine"


def _default_permanent_store() -> Path:
    return Path.home() / ".local" / "share" / "media_engine"


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
    # Where the engine downloads / caches ML model weights (mlx-whisper,
    # mlx-lm, sentence-transformers, pyannote). Defaults to
    # ``{permanent_store}/models`` so model files live on the same volume
    # as artifacts — keeps them off the internal SSD on machines with a
    # mounted external. When set, EngineConfig.load auto-exports
    # ``HF_HOME = models_dir/huggingface`` (if HF_HOME isn't already
    # set) so every HuggingFace-backed backend shares the same cache.
    #
    # Why this matters: MLX uses unified memory + downloads via HF Hub.
    # An M-series Mac with a near-full internal SSD will thrash + freeze
    # when the model load triggers swap. Pointing models off the
    # internal SSD removes the disk pressure half of that failure mode.
    models_dir: Path | None = None
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
    # Phase 7 — acoustic speaker identity, privacy-by-default. Voice
    # fingerprints are biometric data, so both are OFF by default:
    #   * ``speaker_storage_enabled`` — gate persisting SpeakerProfile
    #     centroids to the fingerprint DB (``speakers.cluster`` still runs
    #     and returns profiles, it just doesn't write them). Reconciliation
    #     against saved profiles also requires this.
    #   * ``speaker_export_enabled`` — gate the ``speakers.*`` ops over the
    #     REST ``/run`` surface (403 when off). MCP already hides them by
    #     default (not in the read-only allow-set).
    # Env: MEDIA_ENGINE_SPEAKER_STORAGE_ENABLED / _SPEAKER_EXPORT_ENABLED.
    speaker_storage_enabled: bool = False
    speaker_export_enabled: bool = False
    # Phase 6: cap on a single ``POST /acquire/upload`` body to prevent
    # a malicious client from filling the disk through the new web-UI
    # multipart endpoint. The upload streams to a tmp file and aborts
    # past this limit; the rest of the engine (acquire.upload op, daemon
    # client, CLI ingest) is unaffected.
    max_upload_mb: int = 2048

    @classmethod
    def load(cls, config_file: Path | None = None) -> EngineConfig:
        """Load config from a TOML file (default ``~/.config/media_engine/config.toml``),
        then merge environment variables on top.

        Before the BaseSettings constructor runs, ``secrets.env`` (if
        present in the config dir) is exported into ``os.environ`` so
        operator-managed secrets are visible both to ``MEDIA_ENGINE_*``
        settings parsing and to downstream ``BackendRequirements`` env
        probes.
        """
        # Local import: avoid a runtime <-> top-level config cycle.
        from media_engine.runtime.secrets import load_secrets

        # Resolve config_dir from env first — the secrets file lives
        # there, and we need to read it BEFORE the BaseSettings ctor
        # runs (otherwise it can't see env vars sourced from the
        # file). Honors the same MEDIA_ENGINE_CONFIG_DIR override as
        # the eventual EngineConfig field.
        env_config_dir = os.environ.get("MEDIA_ENGINE_CONFIG_DIR")
        secrets_dir = Path(env_config_dir) if env_config_dir else _default_config_dir()
        load_secrets(secrets_dir)

        toml_data: dict[str, Any] = {}
        if config_file is None:
            default_toml = _default_config_dir() / "config.toml"
            if default_toml.exists():
                config_file = default_toml
        if config_file is not None and config_file.exists():
            with config_file.open("rb") as f:
                toml_data = tomllib.load(f)
        cfg = cls(**toml_data)

        # Auto-export HF_HOME so every HuggingFace-backed backend
        # (mlx-whisper, mlx-lm, sentence-transformers, pyannote)
        # caches to the same on-disk location — the resolved
        # models_dir, which lives off the internal SSD by default.
        # We only set HF_HOME when the operator hasn't already (env or
        # secrets.env), so an explicit override always wins.
        if "HF_HOME" not in os.environ:
            os.environ["HF_HOME"] = str(cfg.resolve_models_dir() / "huggingface")

        return cfg

    def resolve_cache_db_url(self) -> str:
        if self.cache_db_url is not None:
            return self.cache_db_url
        return f"sqlite+pysqlite:///{self.permanent_store / 'cache.db'}"

    def resolve_models_dir(self) -> Path:
        """Effective models-cache directory.

        Operator-set ``models_dir`` wins; otherwise falls back to a
        ``models/`` subdirectory of ``permanent_store``. The directory
        is NOT created lazily here — ``validate_storage`` (and the
        first HuggingFace download) will create it on demand.
        """
        if self.models_dir is not None:
            return self.models_dir
        return self.permanent_store / "models"

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


class ConfigValidationError(ValueError):
    """Raised when ``config.toml`` text is syntactically or semantically invalid.

    Surfaced by the Web UI's config editor (``PUT /settings/config-files``)
    as a 422 with the parse/validation message so the operator never
    persists a config the engine would reject at next boot.
    """


def _config_allowed_keys() -> set[str]:
    """Top-level keys a valid ``config.toml`` may set — field names plus any
    validation aliases (e.g. ``cache_db_url`` also accepts ``MEDIA_ENGINE_DB_URL``)."""
    allowed: set[str] = set(EngineConfig.model_fields)
    for field in EngineConfig.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            allowed.update(c for c in alias.choices if isinstance(c, str))
        elif isinstance(alias, str):
            allowed.add(alias)
    return allowed


def validate_config_toml(text: str) -> None:
    """Parse + round-trip ``config.toml`` text through ``EngineConfig``.

    Raises :class:`ConfigValidationError` on a TOML syntax error, an
    unknown top-level key (``extra="ignore"`` would otherwise swallow a
    typo like ``permanant_store`` and silently ignore it), or a value
    that fails field validation. Empty text is valid (resets to defaults).
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigValidationError(f"invalid TOML: {e}") from e
    unknown = sorted(set(data) - _config_allowed_keys())
    if unknown:
        raise ConfigValidationError(
            f"unknown config key(s): {unknown}. Allowed keys are the EngineConfig "
            f"fields — see `med config` for the effective set."
        )
    try:
        EngineConfig(**data)
    except ValidationError as e:
        raise ConfigValidationError(f"invalid config value — {e}") from e


def write_config_toml(config_dir: Path, text: str) -> Path:
    """Validate then atomically write ``{config_dir}/config.toml``.

    Validates first (raises :class:`ConfigValidationError`), then writes via
    a same-dir temp file + ``os.replace`` so a crash mid-write can't leave a
    truncated config. Returns the written path.
    """
    validate_config_toml(text)
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "config.toml"
    tmp = target.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return target
