"""Operation registry — module-level dict + ``@register_op`` decorator.

Op names must follow ``<group>.<verb>`` (lowercase, dots-separated, no extra
dots) — enforced at registration. Duplicate registration raises.
"""

from __future__ import annotations

import re
from typing import TypeVar

from ._base import Operation

_OP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

T = TypeVar("T", bound=type[Operation])


class OpRegistry:
    """Global registry. Use the module-level helpers (``register_op``,
    ``get``, ``list_all``); ``OpRegistry`` is a namespace."""

    _ops: dict[str, type[Operation]] = {}

    @classmethod
    def register(cls, op_class: type[Operation]) -> type[Operation]:
        name = op_class.name
        if not _OP_NAME_RE.match(name):
            raise ValueError(
                f"Operation name must match <group>.<verb> "
                f"(lowercase, snake_case, single dot): got {name!r}"
            )
        if name in cls._ops and cls._ops[name] is not op_class:
            raise ValueError(
                f"Operation {name!r} already registered as {cls._ops[name].__qualname__}"
            )
        cls._ops[name] = op_class
        return op_class

    @classmethod
    def get(cls, name: str) -> type[Operation]:
        try:
            return cls._ops[name]
        except KeyError as e:
            raise LookupError(f"No operation registered with name {name!r}") from e

    @classmethod
    def list_all(cls) -> list[type[Operation]]:
        return sorted(cls._ops.values(), key=lambda op: op.name)

    @classmethod
    def has(cls, name: str) -> bool:
        return name in cls._ops

    @classmethod
    def clear(cls) -> None:
        """For tests only — wipe the registry."""
        cls._ops.clear()


def register_op(op_class: T) -> T:
    """Decorator: register an Operation subclass."""
    OpRegistry.register(op_class)
    return op_class
