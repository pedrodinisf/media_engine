"""``med web start`` — boot the local Web UI (Phase 6).

The Web UI is a SvelteKit SPA built ahead of time and bundled into the
package under ``media_engine/web/dist/``. ``med web start`` is a thin
wrapper over ``med api start`` semantics that validates the dist tree
is present, boots uvicorn, and (optionally) opens the browser at
``http://<host>:<port>/ui/``.

`med api start` and `med web start` are intentionally distinct verbs
(plan §12 open follow-up): API stays headless-by-default for CI /
production deploys; ``web start`` is the path of least resistance for
the local-first user who just wants the UI to come up.
"""

from __future__ import annotations

import os
import webbrowser
from typing import Annotated

import typer
from rich.console import Console

from media_engine.api.app import ui_dist_dir

app = typer.Typer(name="web", help="Phase 6 local Web UI lifecycle.")
console = Console()
err_console = Console(stderr=True)


def _should_open_browser(open_flag: bool | None) -> bool:
    """Decide whether to call ``webbrowser.open``.

    Explicit ``--open`` / ``--no-open`` always wins. Otherwise auto-open
    only when a display is plausibly available — i.e. on macOS / Windows
    by default, and on Linux when ``DISPLAY`` or ``WAYLAND_DISPLAY`` is
    set. ``MEDIA_ENGINE_NO_BROWSER=1`` is the universal opt-out (Docker
    containers, CI smoke tests).
    """
    if open_flag is not None:
        return open_flag
    if os.environ.get("MEDIA_ENGINE_NO_BROWSER"):
        return False
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    # Default-true on darwin/win; default-false on headless linux.
    import sys

    return sys.platform in {"darwin", "win32"}


@app.command("start")
def cmd_web_start(
    host: Annotated[
        str, typer.Option("--host", help="Bind address")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="TCP port")] = 8000,
    open_browser: Annotated[
        bool | None,
        typer.Option(
            "--open/--no-open",
            help="Open the browser at /ui/ after boot. Auto-detects display.",
        ),
    ] = None,
) -> None:
    """Boot the FastAPI app + Svelte SPA on ``http://host:port/ui/``.

    Validates that ``media_engine/web/dist/index.html`` exists before
    handing off to uvicorn so a missing build fails fast with a clear
    install hint.
    """
    dist = ui_dist_dir()
    if not (dist / "index.html").is_file():
        err_console.print(
            "[red]Web UI dist not found at[/red] "
            f"[cyan]{dist}[/cyan]\n\n"
            "Build it from the repo root:\n"
            "  [bold]pnpm -C web install --frozen-lockfile[/bold]\n"
            "  [bold]pnpm -C web build[/bold]\n\n"
            "Then retry [bold]med web start[/bold]."
        )
        raise typer.Exit(1)

    try:
        import uvicorn
    except ImportError as e:
        err_console.print(
            "[red]API extra not installed. Run `uv sync --extra api`.[/red]"
        )
        raise typer.Exit(1) from e

    if _should_open_browser(open_browser):
        url = f"http://{host}:{port}/ui/"
        # Open *after* uvicorn binds; uvicorn.run blocks, so we schedule
        # via a tiny background timer.
        import threading

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        console.print(
            f"[green]Web UI[/green] → [cyan]{url}[/cyan] (opening browser)"
        )
    else:
        console.print(
            f"[green]Web UI[/green] → [cyan]http://{host}:{port}/ui/[/cyan]"
        )

    from media_engine.api.app import build_app

    uvicorn.run(build_app(), host=host, port=port)
