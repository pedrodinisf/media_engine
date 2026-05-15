"""JSON-line wire protocol for the Unix-socket daemon.

Each request and each response is a single line of UTF-8 JSON terminated by
``\\n``. Both directions are versioned via ``protocol_version``; the server
refuses requests whose major version differs.

Streaming events use a long-lived ``subscribe_events`` request: the server
keeps the connection open and pushes ``EventNotification`` frames as ops
emit them; the client reads until EOF or sends a ``unsubscribe_events``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, Field
from pydantic import TypeAdapter as _TypeAdapter

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.runtime.events import Event
from media_engine.runtime.lineage import LineageNode

PROTOCOL_VERSION: str = "1.0"


class _Frame(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    request_id: str | None = None  # echoed back on responses for client correlation


# ─────────────────────────────────────────────────────────────────
# Requests
# ─────────────────────────────────────────────────────────────────


class PingRequest(_Frame):
    type: Literal["ping"] = "ping"


class StatusRequest(_Frame):
    type: Literal["status"] = "status"


class RunOpRequest(_Frame):
    type: Literal["run_op"] = "run_op"
    op_name: str
    inputs: list[str] = []
    backend: str | None = None
    params: dict[str, Any] = {}


class GetArtifactRequest(_Frame):
    type: Literal["get_artifact"] = "get_artifact"
    artifact_id: str


class ListArtifactsRequest(_Frame):
    type: Literal["list_artifacts"] = "list_artifacts"
    kind: Kind | None = None
    limit: int = 100


class LineageRequest(_Frame):
    type: Literal["lineage"] = "lineage"
    artifact_id: str
    max_depth: int = 10


class ResolveIdRequest(_Frame):
    type: Literal["resolve_id"] = "resolve_id"
    prefix: str


class SubscribeEventsRequest(_Frame):
    type: Literal["subscribe_events"] = "subscribe_events"


Request: TypeAlias = Annotated[
    PingRequest
    | StatusRequest
    | RunOpRequest
    | GetArtifactRequest
    | ListArtifactsRequest
    | LineageRequest
    | ResolveIdRequest
    | SubscribeEventsRequest,
    Field(discriminator="type"),
]


# ─────────────────────────────────────────────────────────────────
# Responses
# ─────────────────────────────────────────────────────────────────


class PingResponse(_Frame):
    type: Literal["pong"] = "pong"
    daemon_pid: int
    started_at: datetime


class StatusResponse(_Frame):
    type: Literal["status_response"] = "status_response"
    daemon_pid: int
    started_at: datetime
    uptime_seconds: float
    namespace: str
    permanent_store: str
    loaded_models: list[str]
    subscriber_count: int


class RunOpResponse(_Frame):
    type: Literal["run_op_response"] = "run_op_response"
    artifacts: list[AnyArtifact]


class GetArtifactResponse(_Frame):
    type: Literal["get_artifact_response"] = "get_artifact_response"
    artifact: AnyArtifact | None


class ListArtifactsResponse(_Frame):
    type: Literal["list_artifacts_response"] = "list_artifacts_response"
    artifacts: list[AnyArtifact]


class LineageResponse(_Frame):
    type: Literal["lineage_response"] = "lineage_response"
    node: LineageNode | None


class ResolveIdResponse(_Frame):
    type: Literal["resolve_id_response"] = "resolve_id_response"
    artifact_id: str


class EventNotification(_Frame):
    type: Literal["event"] = "event"
    event: Event


class ErrorResponse(_Frame):
    type: Literal["error"] = "error"
    error_class: str
    message: str
    traceback: str | None = None


Response: TypeAlias = Annotated[
    PingResponse
    | StatusResponse
    | RunOpResponse
    | GetArtifactResponse
    | ListArtifactsResponse
    | LineageResponse
    | ResolveIdResponse
    | EventNotification
    | ErrorResponse,
    Field(discriminator="type"),
]


_request_adapter: _TypeAdapter[Request] = _TypeAdapter(Request)
_response_adapter: _TypeAdapter[Response] = _TypeAdapter(Response)


def encode_frame(frame: BaseModel) -> bytes:
    """Serialize a frame to a JSON line (with trailing newline)."""
    return frame.model_dump_json().encode("utf-8") + b"\n"


def decode_request(line: bytes) -> Request:
    return _request_adapter.validate_json(line)


def decode_response(line: bytes) -> Response:
    return _response_adapter.validate_json(line)
