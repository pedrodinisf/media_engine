"""``med acquire-live`` — record a live HLS stream, hotkey-segmentable.

Runs ``acquire.livestream`` and, while it records, lets the user carve a
new segment on demand. Two triggers, both calling
``ffmpeg_recorder.request_split_all()`` from the main thread:

* ``SIGUSR1`` — always available (``kill -USR1 <pid>``);
* an optional ``pynput`` keyboard hotkey (``--hotkey "cmd+shift+j"``),
  soft-failing to SIGUSR1-only when ``pynput`` isn't installed
  (``uv sync --extra live``).
"""

from __future__ import annotations

import asyncio
import json as _json
import signal
import threading
from typing import Annotated, Any

import typer
from rich.console import Console

from media_engine.config import EngineConfig

console = Console()
err_console = Console(stderr=True)


def _to_pynput_hotkey(spec: str) -> str:
    """``"cmd+shift+j"`` → pynput's ``"<cmd>+<shift>+j"`` syntax."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    mods = {"cmd", "ctrl", "alt", "shift", "cmd_l", "ctrl_l", "alt_l"}
    return "+".join(f"<{p}>" if p in mods else p for p in parts)


def _start_hotkey_listener(spec: str, on_fire: Any) -> Any | None:
    """Best-effort pynput listener. Returns the listener (to stop) or None."""
    try:
        from pynput import keyboard  # type: ignore  # noqa: PGH003
    except ImportError:
        err_console.print(
            "[yellow]pynput not installed — hotkey disabled; use "
            "`kill -USR1 <pid>` to segment. Install: "
            "uv sync --extra live[/yellow]"
        )
        return None
    hk = keyboard.HotKey(keyboard.HotKey.parse(_to_pynput_hotkey(spec)), on_fire)

    def _on_press(k: Any) -> None:
        if k is not None:
            hk.press(listener.canonical(k))

    def _on_release(k: Any) -> None:
        if k is not None:
            hk.release(listener.canonical(k))

    listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
    listener.start()
    console.print(f"[green]Hotkey armed: press {spec} to start a new segment[/green]")
    return listener


def cmd_acquire_live(
    url: Annotated[str, typer.Argument(help="Livestream page URL or .m3u8")],
    hotkey: Annotated[
        str | None,
        typer.Option("--hotkey", help='Segment hotkey, e.g. "cmd+shift+j"'),
    ] = None,
    max_duration: Annotated[
        int | None,
        typer.Option("--max-duration", help="Hard stop after N seconds"),
    ] = None,
    segment_seconds: Annotated[
        int | None,
        typer.Option("--segment-seconds", help="Auto-split every N seconds"),
    ] = None,
    quality: Annotated[str, typer.Option("--quality")] = "best",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON")
    ] = False,
) -> None:
    """Record a live HLS stream into one or more segment Videos."""
    from media_engine.backends.acquire.ffmpeg_recorder import request_split_all

    cfg = EngineConfig.load()

    def _fire() -> None:
        n = request_split_all()
        console.print(f"[cyan]✂️  segment boundary requested ({n} recorder(s))[/cyan]")

    # SIGUSR1 → split (main-thread only; restore on exit).
    prev_handler: Any = None
    have_usr1 = hasattr(signal, "SIGUSR1") and (
        threading.current_thread() is threading.main_thread()
    )
    def _on_usr1(_signum: int, _frame: Any) -> None:
        _fire()

    if have_usr1:
        try:
            prev_handler = signal.getsignal(signal.SIGUSR1)
            signal.signal(signal.SIGUSR1, _on_usr1)
        except (ValueError, OSError):
            have_usr1 = False

    listener = _start_hotkey_listener(hotkey, _fire) if hotkey else None

    async def _go() -> int:
        from media_engine.cli._handle import open_handle

        async with open_handle(cfg) as h:
            try:
                outputs = await h.run(
                    "acquire.livestream",
                    url=url,
                    quality=quality,
                    max_duration_sec=max_duration,
                    segment_seconds=segment_seconds,
                )
            except Exception as e:
                err_console.print(f"[red]acquire.livestream failed: {e}[/red]")
                return 1
            if json_output:
                typer.echo(_json.dumps([a.id for a in outputs], indent=2))
            else:
                console.print(
                    f"[green]Recorded {len(outputs)} segment(s)[/green]"
                )
                for a in outputs:
                    typer.echo(a.id)
            return 0

    try:
        code = asyncio.run(_go())
    finally:
        if listener is not None:
            listener.stop()
        if have_usr1:
            signal.signal(signal.SIGUSR1, prev_handler)
    raise typer.Exit(code)
