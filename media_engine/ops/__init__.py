"""Operations — capability-named verbs.

The public API for op authors:

    from media_engine.ops import Operation, OperationContext, CostEstimate, register_op

Concrete ops live under ``ops/<group>/<verb>.py`` (e.g.
``ops/audio/transcribe.py``).
"""

from ._base import CostEstimate, Operation, OperationContext
from ._registry import OpRegistry, register_op

__all__ = [
    "CostEstimate",
    "OpRegistry",
    "Operation",
    "OperationContext",
    "register_op",
]
