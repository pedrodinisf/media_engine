"""Async client for the Unix-socket daemon.

Used by the CLI (``med ...``) to auto-route requests through a running
daemon for warm-model speed. ``DaemonClient.connect()`` returns ``None``
if the socket is missing or the daemon doesn't pong within ``timeout``,
letting the caller fall back to ``Engine.open_quick``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.runtime.events import Event
from media_engine.runtime.lineage import LineageNode

from .protocol import (
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
    ResolveIdRequest,
    ResolveIdResponse,
    Response,
    RunOpRequest,
    RunOpResponse,
    StatusRequest,
    StatusResponse,
    SubscribeEventsRequest,
    decode_response,
    encode_frame,
)


class DaemonClientError(RuntimeError):
    """Raised when a request returns ``ErrorResponse`` from the daemon."""


class DaemonClient:
    """Async JSON-line client over a Unix socket."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(
        cls, socket_path: Path, timeout: float = 0.05
    ) -> DaemonClient | None:
        """Try to connect; ``ping`` round-trip must complete within ``timeout``.

        Returns ``None`` if the socket doesn't exist, can't be opened, or
        doesn't pong in time.
        """
        if not socket_path.exists():
            return None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(path=str(socket_path)),
                timeout=timeout,
            )
        except (TimeoutError, FileNotFoundError, ConnectionRefusedError, OSError):
            return None
        client = cls(reader, writer)
        try:
            await asyncio.wait_for(client.ping(), timeout=timeout)
        except (TimeoutError, DaemonClientError, Exception):
            await client.close()
            return None
        return client

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self._writer.close()
            await self._writer.wait_closed()

    async def __aenter__(self) -> DaemonClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ── Request helpers ──

    async def _send(self, frame: Any) -> None:
        self._writer.write(encode_frame(frame))
        await self._writer.drain()

    async def _recv(self) -> Response:
        line = await self._reader.readline()
        if not line:
            raise DaemonClientError("daemon closed connection")
        return decode_response(line)

    async def _round_trip(self, frame: Any) -> Response:
        await self._send(frame)
        return await self._recv()

    @staticmethod
    def _unwrap(response: Response, expected: type) -> Any:
        if isinstance(response, ErrorResponse):
            raise DaemonClientError(
                f"{response.error_class}: {response.message}"
            )
        if not isinstance(response, expected):
            raise DaemonClientError(
                f"expected {expected.__name__}, got {type(response).__name__}"
            )
        return response

    # ── Public surface ──

    async def ping(self) -> PingResponse:
        resp = await self._round_trip(PingRequest(request_id=uuid4().hex))
        return self._unwrap(resp, PingResponse)

    async def status(self) -> StatusResponse:
        resp = await self._round_trip(StatusRequest(request_id=uuid4().hex))
        return self._unwrap(resp, StatusResponse)

    async def run_op(
        self,
        op_name: str,
        *,
        inputs: list[str] | None = None,
        backend: str | None = None,
        **params: Any,
    ) -> list[AnyArtifact]:
        resp = await self._round_trip(
            RunOpRequest(
                request_id=uuid4().hex,
                op_name=op_name,
                inputs=inputs or [],
                backend=backend,
                params=params,
            )
        )
        return self._unwrap(resp, RunOpResponse).artifacts

    async def get_artifact(self, artifact_id: str) -> AnyArtifact | None:
        resp = await self._round_trip(
            GetArtifactRequest(request_id=uuid4().hex, artifact_id=artifact_id)
        )
        return self._unwrap(resp, GetArtifactResponse).artifact

    async def list_artifacts(
        self, kind: Kind | None = None, limit: int = 100
    ) -> list[AnyArtifact]:
        resp = await self._round_trip(
            ListArtifactsRequest(request_id=uuid4().hex, kind=kind, limit=limit)
        )
        return self._unwrap(resp, ListArtifactsResponse).artifacts

    async def lineage(
        self, artifact_id: str, max_depth: int = 10
    ) -> LineageNode | None:
        resp = await self._round_trip(
            LineageRequest(
                request_id=uuid4().hex,
                artifact_id=artifact_id,
                max_depth=max_depth,
            )
        )
        return self._unwrap(resp, LineageResponse).node

    async def resolve_id(self, prefix: str) -> str:
        resp = await self._round_trip(
            ResolveIdRequest(request_id=uuid4().hex, prefix=prefix)
        )
        return self._unwrap(resp, ResolveIdResponse).artifact_id

    async def subscribe_events(self) -> AsyncIterator[Event]:
        await self._send(SubscribeEventsRequest(request_id=uuid4().hex))

        async def _iter() -> AsyncIterator[Event]:
            while True:
                resp = await self._recv()
                if isinstance(resp, ErrorResponse):
                    raise DaemonClientError(
                        f"{resp.error_class}: {resp.message}"
                    )
                if isinstance(resp, EventNotification):
                    yield resp.event
                # silently skip non-event frames (shouldn't happen)

        return _iter()
