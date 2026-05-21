"""``/health`` and ``/ready`` endpoints.

Unlike the rest of the surface these are **un-authenticated** — they
exist for kubelet probes / load balancers and need to respond without
a token. They return 200 / 503 status with a JSON body describing
each dependency check.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Response, status

from media_engine.runtime.health import liveness, readiness

router = APIRouter()


@router.get("/health")
def get_health() -> Response:
    report = liveness()
    return Response(
        content=json.dumps(report.to_dict()),
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )


@router.get("/ready")
def get_ready() -> Response:
    report = readiness()
    code = status.HTTP_200_OK if report.ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return Response(
        content=json.dumps(report.to_dict()),
        media_type="application/json",
        status_code=code,
    )
