"""Daemon — long-lived ``Engine.open_session`` exposed over JSON-RPC.

Public surface:
    from media_engine.daemon import DaemonServer, DaemonClient
"""

from .client import DaemonClient
from .server import DaemonServer

__all__ = ["DaemonClient", "DaemonServer"]
