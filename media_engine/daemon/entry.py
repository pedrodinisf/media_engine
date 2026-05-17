"""Entry module for ``python -m media_engine.daemon.entry``.

Used by ``med daemon start`` (background mode) so the daemon process is a
distinct Python process — survives the parent CLI exiting and is
inspectable via PID alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

from media_engine.bootstrap import register_all
from media_engine.config import EngineConfig
from media_engine.daemon import DaemonServer
from media_engine.runtime.engine import Engine

# Full op + backend catalog so the daemon can dispatch everything.
register_all()


async def _serve(cfg: EngineConfig, socket_path: Path) -> None:
    engine = Engine.open_session(cfg)
    server = DaemonServer(engine, socket_path)
    await server.start()
    print(f"daemon listening on {socket_path} (pid {os.getpid()})", flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        serve_task = asyncio.create_task(server.serve_forever())
        await stop_event.wait()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
    finally:
        await server.stop()
        engine.close()


def main() -> None:
    cfg = EngineConfig.load()
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.validate_storage()
    socket_path = cfg.daemon_socket or (cfg.config_dir / "daemon.sock")
    try:
        asyncio.run(_serve(cfg, socket_path))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
