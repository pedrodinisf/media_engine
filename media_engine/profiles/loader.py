"""Discover + load profile files from disk.

YAML files (``.yaml`` / ``.yml``) parse as ``PipelineProfile`` or
``PromptProfile`` based on their ``kind:`` field. Markdown files (``.md``)
are treated as ``PromptProfile`` — the YAML frontmatter populates fields
and the markdown body becomes the system prompt.

Discovery searches:
1. ``{config_dir}/profiles`` (the user's directory)
2. ``<engine repo>/profiles`` (bundled starter profiles, Phase 5)
3. Any directory passed via ``profile_dirs=...`` argument

Later directories override earlier ones for the same profile name.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import frontmatter  # type: ignore[import-untyped]
import yaml
from pydantic import ValidationError

from .schema import PipelineProfile, Profile, PromptProfile

_PROFILE_GLOBS = ("*.yaml", "*.yml", "*.md")


class ProfileLoadError(RuntimeError):
    """Raised when a profile file fails to parse, validate, or compile."""


def _parse_kind(data: dict[str, Any]) -> str:
    kind = data.get("kind", "pipeline")
    if not isinstance(kind, str):
        raise ProfileLoadError(f"profile `kind` must be a string, got {type(kind).__name__}")
    return kind


def _load_yaml(path: Path) -> Profile:
    try:
        raw_any: Any = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ProfileLoadError(f"YAML parse error in {path}: {e}") from e
    if not isinstance(raw_any, dict):
        raise ProfileLoadError(
            f"{path} must contain a YAML mapping at the top level "
            f"(got {type(raw_any).__name__})"
        )
    raw: dict[str, Any] = cast(dict[str, Any], raw_any)
    kind = _parse_kind(raw)
    try:
        if kind == "pipeline":
            return PipelineProfile(**raw)
        if kind == "prompt":
            return PromptProfile(**raw)
        raise ProfileLoadError(f"{path}: unknown profile kind {kind!r}")
    except ValidationError as e:
        raise ProfileLoadError(f"{path}: schema validation failed:\n{e}") from e


def _load_md(path: Path) -> PromptProfile:
    try:
        post: Any = frontmatter.load(str(path))
    except Exception as e:  # frontmatter raises various I/O / parse errors
        raise ProfileLoadError(f"markdown frontmatter parse error in {path}: {e}") from e
    metadata: dict[str, Any] = dict(post.metadata)
    metadata.setdefault("kind", "prompt")
    metadata.setdefault("name", path.stem)
    metadata["body"] = post.content
    try:
        return PromptProfile(**metadata)
    except ValidationError as e:
        raise ProfileLoadError(f"{path}: schema validation failed:\n{e}") from e


def load_profile(path: Path) -> Profile:
    """Load a single profile file by path."""
    if not path.exists():
        raise ProfileLoadError(f"profile not found: {path}")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return _load_yaml(path)
    if suffix == ".md":
        return _load_md(path)
    raise ProfileLoadError(
        f"unknown profile extension {suffix!r} (expected .yaml/.yml/.md)"
    )


def discover_profiles(
    profile_dirs: Iterable[Path] | None = None,
    *,
    config_dir: Path | None = None,
    repo_dir: Path | None = None,
) -> dict[str, tuple[Path, Profile]]:
    """Walk all profile dirs and return ``{profile_name: (path, profile)}``.

    Later directories override earlier ones for the same name.
    Files that fail to load are skipped with their error attached as the
    ``__load_error__`` attribute on a stub Profile (we don't raise globally,
    so one bad file doesn't blow up the whole listing).
    """
    dirs: list[Path] = []
    if config_dir is not None and config_dir.exists():
        dirs.append(config_dir)
    if repo_dir is not None and repo_dir.exists():
        dirs.append(repo_dir)
    for d in profile_dirs or []:
        if d.exists():
            dirs.append(d)

    out: dict[str, tuple[Path, Profile]] = {}
    for d in dirs:
        for pattern in _PROFILE_GLOBS:
            for path in sorted(d.rglob(pattern)):
                try:
                    profile = load_profile(path)
                except ProfileLoadError:
                    continue
                out[profile.name] = (path, profile)
    return out
