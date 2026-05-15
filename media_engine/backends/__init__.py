"""Backend implementations — per-op pluggable implementations.

Public API for backend authors:

    from media_engine.backends import Backend, BackendRegistry, BackendRequirements

Concrete backends live under ``backends/<group>_<verb>/<provider>.py`` (e.g.
``backends/transcribe/mlx_whisper.py``, ``backends/video_multimodal/gemini.py``).
"""

from ._base import Backend, BackendRegistry, BackendRequirements, register_backend

__all__ = [
    "Backend",
    "BackendRegistry",
    "BackendRequirements",
    "register_backend",
]
