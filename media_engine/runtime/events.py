"""Event types emitted by Operations.

Phase 0 ships only the type definitions. The full EventBus arrives in Phase 1
(commit 14) alongside the DAG executor and daemon. Keeping the types here so
``OperationContext.emit`` has a real signature from the start.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field

from media_engine.artifacts import AnyArtifact


class _BaseEvent(BaseModel):
    event_id: str
    op_run_id: str
    job_id: str | None = None
    artifact_id: str | None = None
    timestamp: datetime


class OpStarted(_BaseEvent):
    type: Literal["op_started"] = "op_started"
    op_name: str
    inputs: list[str] = []
    params: dict[str, object] = {}


class Progress(_BaseEvent):
    type: Literal["progress"] = "progress"
    fraction: float
    message: str = ""
    phase: str | None = None


class ArtifactReady(_BaseEvent):
    type: Literal["artifact_ready"] = "artifact_ready"
    artifact: AnyArtifact


class OpCompleted(_BaseEvent):
    type: Literal["op_completed"] = "op_completed"
    outputs: list[str] = []
    duration_seconds: float
    cost: dict[str, float] = {}


class OpFailed(_BaseEvent):
    type: Literal["op_failed"] = "op_failed"
    error_class: str
    message: str
    retryable: bool = False
    suggested_action: str = ""
    traceback: str | None = None


class LogLine(_BaseEvent):
    type: Literal["log_line"] = "log_line"
    level: str
    source: str
    line: str


Event: TypeAlias = Annotated[
    OpStarted | Progress | ArtifactReady | OpCompleted | OpFailed | LogLine,
    Field(discriminator="type"),
]
