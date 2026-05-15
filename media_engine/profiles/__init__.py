"""Profile loader + Pipeline compiler.

A profile is a YAML or markdown-with-frontmatter file describing a named,
parameterized pipeline. Two flavors:

- ``kind: pipeline`` — explicit DAG of op invocations. Compiles to a
  ``runtime.dag.Pipeline``.
- ``kind: prompt`` — single-op shorthand (default ``video.multimodal``)
  with a system prompt embedded. Compiles to a one-node Pipeline.

Profiles are auto-discovered from ``{config_dir}/profiles``,
``<repo>/profiles`` (when present), and any path passed via ``--profile-dir``.
"""

from .loader import (
    ProfileLoadError,
    discover_profiles,
    load_profile,
)
from .schema import (
    GraphNodeSpec,
    InputSpec,
    PipelineProfile,
    Profile,
    PromptProfile,
)

__all__ = [
    "GraphNodeSpec",
    "InputSpec",
    "PipelineProfile",
    "Profile",
    "ProfileLoadError",
    "PromptProfile",
    "discover_profiles",
    "load_profile",
]
