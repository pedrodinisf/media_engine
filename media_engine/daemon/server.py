"""``DaemonServer`` — asyncio Unix-socket dispatch loop.

One server, many connections, all sharing one ``Engine.open_session()``.
Each request is a single JSON line; each response is a single JSON line.
``subscribe_events`` keeps the connection open and pushes EventNotification
frames as the engine's ``EventBus`` emits them.

Protocol-version mismatches are returned as ``ErrorResponse`` with a clear
message; clients are expected to ``med daemon stop && med daemon start``
after upgrading.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path

from media_engine.runtime.engine import Engine

from .protocol import (
    PROTOCOL_VERSION,
    ErrorResponse,
    EventNotification,
    GetArtifactRequest,
    GetArtifactResponse,
    LineageRequest,
    LineageResponse,
    ListArtifactsRequest,
    ListArtifactsResponse,
    PingRequest,
    PingResponse,
    Request,
    ResolveIdRequest,
    ResolveIdResponse,
    Response,
    RunOpRequest,
    RunOpResponse,
    StatusRequest,
    StatusResponse,
    SubscribeEventsRequest,
    decode_request,
    encode_frame,
)


class DaemonServer:
    """Long-lived JSON-RPC server bound to a Unix socket."""

    def __init__(self, engine: Engine, socket_path: Path) -> None:
        self.engine = engine
        self.socket_path = socket_path
        self.started_at = datetime.now(UTC)
        self._server: asyncio.AbstractServer | None = None
        self._serving = False

    async def start(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o600)
        self._serving = True

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self._serving:
            return
        self._serving = False
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        if self.socket_path.exists():
            with contextlib.suppress(OSError):
                self.socket_path.unlink()

    # ── Connection handling ──

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    return
                response = await self._dispatch(line, writer)
                if response is not None:
                    writer.write(encode_frame(response))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _dispatch(
        self,
        line: bytes,
        writer: asyncio.StreamWriter,
    ) -> Response | None:
        try:
            req = decode_request(line)
        except Exception as e:
            return ErrorResponse(
                error_class=type(e).__name__,
                message=f"malformed request: {e}",
            )

        if req.protocol_version.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
            return ErrorResponse(
                request_id=req.request_id,
                error_class="ProtocolVersionMismatch",
                message=(
                    f"daemon speaks {PROTOCOL_VERSION}, client sent "
                    f"{req.protocol_version}. Run `med daemon stop && med daemon start`."
                ),
            )

        try:
            return await self._dispatch_typed(req, writer)
        except Exception as e:
            return ErrorResponse(
                request_id=req.request_id,
                error_class=type(e).__name__,
                message=str(e),
                traceback=traceback.format_exc(),
            )

    async def _dispatch_typed(
        self,
        req: Request,
        writer: asyncio.StreamWriter,
    ) -> Response | None:
        if isinstance(req, PingRequest):
            return PingResponse(
                request_id=req.request_id,
                daemon_pid=os.getpid(),
                started_at=self.started_at,
            )
        if isinstance(req, StatusRequest):
            now = datetime.now(UTC)
            return StatusResponse(
                request_id=req.request_id,
                daemon_pid=os.getpid(),
                started_at=self.started_at,
                uptime_seconds=(now - self.started_at).total_seconds(),
                namespace=self.engine.config.namespace,
                permanent_store=str(self.engine.config.permanent_store),
                loaded_models=self.engine.model_pool.keys(),
                subscriber_count=self.engine.event_bus.subscriber_count,
            )
        if isinstance(req, RunOpRequest):
            artifacts = await self.engine.run(
                req.op_name,
                inputs=req.inputs,
                backend=req.backend,
                **req.params,
            )
            return RunOpResponse(request_id=req.request_id, artifacts=artifacts)
        if isinstance(req, GetArtifactRequest):
            return GetArtifactResponse(
                request_id=req.request_id,
                artifact=self.engine.get_artifact(req.artifact_id),
            )
        if isinstance(req, ListArtifactsRequest):
            return ListArtifactsResponse(
                request_id=req.request_id,
                artifacts=self.engine.list_artifacts(kind=req.kind, limit=req.limit),
            )
        if isinstance(req, LineageRequest):
            return LineageResponse(
                request_id=req.request_id,
                node=self.engine.lineage(req.artifact_id, max_depth=req.max_depth),
            )
        if isinstance(req, ResolveIdRequest):
            return ResolveIdResponse(
                request_id=req.request_id,
                artifact_id=self.engine.resolve_id(req.prefix),
            )
        # Final variant: SubscribeEventsRequest (handled inline because its
        # response stream consumes the connection and dispatch returns None).
        await self._stream_events(req, writer)
        return None

    async def _stream_events(
        self,
        req: Request,
        writer: asyncio.StreamWriter,
    ) -> None:
        assert isinstance(req, SubscribeEventsRequest)
        async for event in self.engine.event_bus.subscribe():
            try:
                writer.write(encode_frame(EventNotification(
                    request_id=req.request_id,
                    event=event,
                )))
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                return
