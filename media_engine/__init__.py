"""Universal media-processing engine.

Typed artifacts, composable operations, pluggable backends, content-addressed
caching, async DAG execution.

Public API (plan §4): the names below are the supported import surface —
``from media_engine import Engine, Pipeline, Artifact, Kind, register_op,
register_backend``. Everything else is internal and may move between
releases.
"""

from media_engine.artifacts import AnyArtifact, Artifact, Kind
from media_engine.backends import register_backend
from media_engine.ops import register_op
from media_engine.runtime.dag import Pipeline
from media_engine.runtime.engine import Engine

__version__ = "0.1.0"

__all__ = [
    "AnyArtifact",
    "Artifact",
    "Engine",
    "Kind",
    "Pipeline",
    "__version__",
    "register_backend",
    "register_op",
]
