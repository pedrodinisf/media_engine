"""Every REST endpoint, one module so the surface is greppable.

The route handlers are thin: they auth, marshal request bodies, defer
to the engine / cache / jobs module, and shape the response. Anything
heavier — selection, schema validation, op execution — happens behind
``Engine`` so the CLI/daemon/MCP paths stay in sync with REST.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from media_engine.api._state import AppState
from media_engine.api.auth import (
    create_token,
    list_tokens,
    revoke_token,
    verify_bearer,
)
from media_engine.api.jobs import (
    cancel_job,
    operation_runs_for_job,
    submit_pipeline,
    submit_run_op,
)
from media_engine.api.sse import job_event_stream
from media_engine.artifacts import AnyArtifact, Kind
from media_engine.backends import BackendRegistry
from media_engine.ops import OpRegistry
from media_engine.profiles.loader import (
    ProfileLoadError,
    discover_profiles,
    load_profile,
)
from media_engine.profiles.pipeline import (
    ProfileCompileError,
    compile_profile,
)
from media_engine.profiles.schema import PipelineProfile, PromptProfile
from media_engine.runtime.cache import ApiTokenInfo, Job
from media_engine.runtime.lineage import OperationRunRef

# ─────────────────────────────────────────────────────────────────
# Dependencies
# ─────────────────────────────────────────────────────────────────


def get_state(request: Request) -> AppState:
    """Pull the shared ``AppState`` off the FastAPI app instance.

    The lifespan attaches state to ``app.state.app_state``; routes pull
    it via this dependency so tests can substitute their own without
    monkeypatching module globals.
    """
    state: AppState | None = getattr(request.app.state, "app_state", None)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine not initialized",
        )
    return state


def require_token(
    request: Request, state: Annotated[AppState, Depends(get_state)]
) -> ApiTokenInfo:
    """Verify the ``Authorization: Bearer <token>`` header (or ``?token=`` query param).

    Returns the token row (id + namespace) so route handlers can scope
    their reads/writes to that namespace.

    The ``?token=`` fallback is a Phase 6 concession: browser
    ``EventSource`` cannot send custom headers, so SSE routes need to
    accept the secret in the URL. Plan §13.1 documents the hardening
    path (short-lived job-scoped nonce). The fallback applies to every
    route — non-SSE callers should keep using the header.

    **Namespace contract** (Phase 4): the engine is single-namespace
    per process — every op runs against ``state.engine.config.namespace``.
    A token bound to a different namespace would silently write to the
    engine's namespace while the caller's reads (filtered by
    ``token.namespace``) returned empty, so we reject the mismatch
    eagerly with 403. Multi-tenant deployments run one API process per
    namespace.
    """
    header = request.headers.get("authorization", "")
    scheme, _, raw_token = header.partition(" ")
    # Tolerate ``Bearer  <token>`` (extra whitespace between scheme and
    # secret). Without ``.strip()`` the leading space would be hashed
    # into the lookup, silently 401-ing valid tokens.
    raw_token = raw_token.strip()
    if scheme.lower() != "bearer" or not raw_token:
        # Fall back to ?token= query param for EventSource compatibility.
        raw_token = (request.query_params.get("token") or "").strip()
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    info = verify_bearer(state.cache, raw_token)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or revoked token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if info.namespace != state.engine.config.namespace:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"token namespace {info.namespace!r} does not match this API "
                f"process namespace {state.engine.config.namespace!r}; run a "
                f"separate API instance per namespace, or mint a token for "
                f"the running namespace"
            ),
        )
    return info


# ─────────────────────────────────────────────────────────────────
# Request / response shapes
# ─────────────────────────────────────────────────────────────────


class RunRequest(BaseModel):
    op: str
    inputs: list[str] = Field(default_factory=list)
    backend: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class RunPreviewResponse(BaseModel):
    """Cost preview shape returned by ``POST /run/preview``.

    Mirrors ``CostEstimate`` plus a ``backend`` field so the Web UI run
    panel can display the resolved backend alongside the estimate. The
    ``estimate_seconds_local`` field is the wall-clock proxy for local
    ops; ``estimate_cost_cents`` is non-null only for cloud-billable
    backends.
    """

    op: str
    backend: str | None
    estimate_seconds_local: float
    estimate_cost_cents: float
    estimate_tokens_in: int
    estimate_tokens_out: int


class PipelineSourceSpec(BaseModel):
    name: str
    artifact_id: str


class PipelineRequest(BaseModel):
    """Submit a pipeline by name (server-known profile) OR by inline YAML.

    Exactly one of ``profile_name`` / ``pipeline_yaml`` must be supplied;
    ``sources`` maps the profile's declared input names to artifact ids
    already in the cache.
    """

    profile_name: str | None = None
    pipeline_yaml: str | None = None
    sources: list[PipelineSourceSpec] = Field(
        default_factory=lambda: cast(list[PipelineSourceSpec], [])
    )


class JobAck(BaseModel):
    job_id: str


class JobDetail(BaseModel):
    job: Job
    op_runs: list[OperationRunRef] = Field(
        default_factory=lambda: cast(list[OperationRunRef], [])
    )


class ArtifactPage(BaseModel):
    items: list[AnyArtifact]
    limit: int
    next_offset: int | None = None


class TokenCreateRequest(BaseModel):
    label: str = ""
    namespace: str = "default"


class TokenCreateResponse(BaseModel):
    token_id: str
    label: str
    namespace: str
    secret: str  # shown once at creation time


class ProfileSummary(BaseModel):
    name: str
    kind: Literal["pipeline", "prompt"]
    description: str = ""
    path: str


class OperationSummary(BaseModel):
    name: str
    version: str
    input_kinds: list[str]
    output_kinds: list[str]
    default_backend: str | None
    variadic_inputs: bool


class OperationDetail(OperationSummary):
    description: str
    params_schema: dict[str, Any]
    declared_resources: list[str]
    backends: list[str]


class BackendSummary(BaseModel):
    op_name: str
    name: str
    version: str


class BackendDetail(BackendSummary):
    requires: dict[str, Any]
    health: str


router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# /run — single op (async)
# ─────────────────────────────────────────────────────────────────


@router.post("/run", response_model=JobAck, status_code=status.HTTP_202_ACCEPTED)
async def post_run(
    body: RunRequest,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> JobAck:
    if not OpRegistry.has(body.op):
        raise HTTPException(status_code=400, detail=f"unknown op {body.op!r}")
    if body.backend is not None and not BackendRegistry.has(body.op, body.backend):
        raise HTTPException(
            status_code=400,
            detail=(
                f"backend {body.backend!r} not registered for {body.op!r}; "
                f"available: {BackendRegistry.for_op(body.op) or '(none)'}"
            ),
        )
    job_id = submit_run_op(
        state,
        op_name=body.op,
        inputs=list(body.inputs),
        backend=body.backend,
        params=dict(body.params),
        namespace=token.namespace,
    )
    return JobAck(job_id=job_id)


# ─────────────────────────────────────────────────────────────────
# /run/preview — cost-only, no submission (Phase 6 commit 42)
# ─────────────────────────────────────────────────────────────────


@router.post("/run/preview", response_model=RunPreviewResponse)
def post_run_preview(
    body: RunRequest,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> RunPreviewResponse:
    """Predict the cost of a ``POST /run`` without submitting it.

    The UI's run panel debounces this to ~250 ms so the cost preview
    updates as the user tweaks params. Uses ``Engine.estimate_op_cost``
    which validates the param model + resolves the backend by the same
    precedence the real submission would (explicit > select_backend >
    default), so a mismatched form errors here rather than at run time.
    """
    del token  # only used to gate access; cost is namespace-agnostic
    if not OpRegistry.has(body.op):
        raise HTTPException(status_code=400, detail=f"unknown op {body.op!r}")

    op_cls = OpRegistry.get(body.op)
    try:
        params_model = op_cls.params_model(**body.params)
    except Exception as e:  # noqa: BLE001 — surface validation errors as 422
        raise HTTPException(status_code=422, detail=str(e)) from None

    # Resolve backend the same way Engine.run does.
    backend_name = body.backend or op_cls().select_backend(params_model) or op_cls.default_backend

    try:
        estimate = state.engine.estimate_op_cost(
            body.op,
            inputs=list(body.inputs),
            **body.params,
        )
    except LookupError as e:
        # An input id didn't resolve — most likely the caller hasn't run
        # the upstream op yet. Surface as 404 so the UI can hint.
        raise HTTPException(status_code=404, detail=str(e)) from None

    return RunPreviewResponse(
        op=body.op,
        backend=backend_name,
        estimate_seconds_local=estimate.local_seconds,
        estimate_cost_cents=estimate.cloud_cents,
        estimate_tokens_in=estimate.tokens_in,
        estimate_tokens_out=estimate.tokens_out,
    )


# ─────────────────────────────────────────────────────────────────
# /pipelines — compile + submit (async)
# ─────────────────────────────────────────────────────────────────


@router.post(
    "/pipelines",
    response_model=JobAck,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_pipeline(
    body: PipelineRequest,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> JobAck:
    if (body.profile_name is None) == (body.pipeline_yaml is None):
        raise HTTPException(
            status_code=400,
            detail="exactly one of `profile_name` or `pipeline_yaml` is required",
        )

    if body.profile_name is not None:
        profiles = discover_profiles(
            config_dir=state.engine.config.config_dir / "profiles",
            repo_dir=Path(__file__).resolve().parents[2] / "profiles",
        )
        if body.profile_name not in profiles:
            raise HTTPException(
                status_code=404,
                detail=f"profile {body.profile_name!r} not found",
            )
        _, profile = profiles[body.profile_name]
    else:
        # Inline YAML body — write to a tmp path and reuse the loader so
        # validation + error reporting stays consistent with the on-disk
        # case. The workdir is per-request (uuid suffix) so concurrent
        # submissions with the same token don't trample each other.
        from uuid import uuid4

        tmp = state.engine.storage.ensure_workdir(
            f"inline-{token.id}-{uuid4().hex}"
        )
        tmp_path = tmp / "inline.yaml"
        tmp_path.write_text(body.pipeline_yaml or "", encoding="utf-8")
        try:
            profile = load_profile(tmp_path)
        except ProfileLoadError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        finally:
            import contextlib as _ctx

            with _ctx.suppress(Exception):
                tmp_path.unlink()
            with _ctx.suppress(Exception):
                tmp.rmdir()

    # Resolve sources via the cache (the API surface speaks artifact ids).
    sources: dict[str, AnyArtifact] = {}
    for spec in body.sources:
        art = state.engine.get_artifact(spec.artifact_id)
        if art is None:
            raise HTTPException(
                status_code=404,
                detail=f"source artifact not found: {spec.artifact_id}",
            )
        sources[spec.name] = art

    try:
        pipeline = compile_profile(profile, sources)
    except ProfileCompileError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    job_id = submit_pipeline(
        state,
        pipeline=pipeline,
        namespace=token.namespace,
        pipeline_name=profile.name,
        pipeline_yaml=body.pipeline_yaml,
    )
    return JobAck(job_id=job_id)


# ─────────────────────────────────────────────────────────────────
# /jobs
# ─────────────────────────────────────────────────────────────────


@router.get("/jobs", response_model=list[Job])
def list_jobs_endpoint(
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            pattern="^(pending|running|completed|failed|cancelled)$",
        ),
    ] = None,
    limit: int = 100,
) -> list[Job]:
    return state.cache.list_jobs(
        status=status_filter, namespace=token.namespace, limit=limit
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job_endpoint(
    job_id: str,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> JobDetail:
    job = state.cache.get_job(job_id, namespace=token.namespace)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobDetail(
        job=job,
        op_runs=operation_runs_for_job(state, job.op_run_ids),
    )


@router.get("/jobs/{job_id}/events")
async def get_job_events(
    job_id: str,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> EventSourceResponse:
    job = state.cache.get_job(job_id, namespace=token.namespace)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return EventSourceResponse(
        job_event_stream(state.engine.event_bus, job_id)
    )


@router.get("/events/stream")
async def get_events_stream(
    state: Annotated[AppState, Depends(get_state)],
    _token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> EventSourceResponse:
    """Phase 6 commit 43 — global SSE stream (every job).

    The UI's job dashboard uses this for a cross-job activity tail.
    Per-job consumers should still hit ``GET /jobs/{id}/events``
    (cheaper subscriber on the EventBus side).

    Accepts the ``?token=`` query param (EventSource can't set headers);
    require_token handles both Authorization and the query fallback.
    """
    return EventSourceResponse(job_event_stream(state.engine.event_bus, None))


@router.get("/events/history")
def get_events_history(
    state: Annotated[AppState, Depends(get_state)],
    _token: Annotated[ApiTokenInfo, Depends(require_token)],
    since: Annotated[str | None, Query(description="ISO-8601 timestamp")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, Any]:
    """Persisted event tail — backs the job-dashboard event history pane."""
    from datetime import datetime
    parsed_since: datetime | None = None
    if since is not None:
        try:
            parsed_since = datetime.fromisoformat(since)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"bad ISO timestamp: {e}") from None
    entries = state.engine.event_log_entries(since=parsed_since, limit=limit)
    items: list[dict[str, Any]] = [
        {
            "id": e.id,
            "ts": e.ts.isoformat(),
            "type": e.type,
            "op_run_id": e.op_run_id,
            "op_name": e.op_name,
            "namespace": e.namespace,
            "payload_json": e.payload_json,
        }
        for e in entries
    ]
    return {"items": items, "limit": limit}


@router.delete("/jobs/{job_id}")
async def delete_job_endpoint(
    job_id: str,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> JSONResponse:
    job = state.cache.get_job(job_id, namespace=token.namespace)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    cancelled = await cancel_job(state, job_id)
    return JSONResponse(
        {"job_id": job_id, "cancelled": cancelled, "status": "cancelled"}
    )


# ─────────────────────────────────────────────────────────────────
# /artifacts
# ─────────────────────────────────────────────────────────────────


@router.get("/artifacts", response_model=ArtifactPage)
def list_artifacts_endpoint(
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
    kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ArtifactPage:
    """Paginated artifact list, newest first.

    Offset-based pagination: pass ``?offset=<n>&limit=<m>`` to walk a
    large cache page by page. We over-fetch by one row to detect
    whether there's a next page without a separate COUNT query — if
    we get back ``limit + 1`` rows, the surplus is dropped from the
    response and the client knows to call again with
    ``offset = current_offset + limit``.
    """
    kind_filter: Kind | None = None
    if kind is not None:
        try:
            kind_filter = Kind(kind.lower())
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"unknown kind: {kind!r}"
            ) from e
    items = state.cache.list_artifacts(
        kind=kind_filter,
        limit=limit + 1,
        offset=offset,
        namespace=token.namespace,
    )
    has_more = len(items) > limit
    if has_more:
        items = items[:limit]
    return ArtifactPage(
        items=items,
        limit=limit,
        next_offset=(offset + limit) if has_more else None,
    )


@router.get("/artifacts/{artifact_id}", response_model=AnyArtifact)
def get_artifact_endpoint(
    artifact_id: str,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> AnyArtifact:
    art = state.cache.get_artifact(artifact_id, namespace=token.namespace)
    if art is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return art


@router.get("/artifacts/{artifact_id}/file")
def get_artifact_file(
    artifact_id: str,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> FileResponse:
    art = state.cache.get_artifact(artifact_id, namespace=token.namespace)
    if art is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    if not Path(art.path).exists():
        raise HTTPException(status_code=410, detail="artifact file missing on disk")
    # FileResponse handles Range requests automatically; we don't need to
    # branch on header here.
    return FileResponse(path=str(art.path), filename=Path(art.path).name)


@router.get("/artifacts/{artifact_id}/lineage")
def get_artifact_lineage(
    artifact_id: str,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
    depth: Annotated[int, Query(ge=0, le=50)] = 10,
) -> dict[str, Any]:
    node = state.cache.lineage_tree(
        artifact_id, namespace=token.namespace, max_depth=depth
    )
    if node is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return node.model_dump(mode="json")


# ─────────────────────────────────────────────────────────────────
# /profiles
# ─────────────────────────────────────────────────────────────────


@router.get("/profiles", response_model=list[ProfileSummary])
def list_profiles_endpoint(
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> list[ProfileSummary]:
    out: list[ProfileSummary] = []
    for name, (path, profile) in discover_profiles(
        config_dir=state.engine.config.config_dir / "profiles",
        repo_dir=Path(__file__).resolve().parents[2] / "profiles",
    ).items():
        out.append(
            ProfileSummary(
                name=name,
                kind=profile.kind,
                description=profile.description,
                path=str(path),
            )
        )
    return out


@router.get("/profiles/{name}")
def get_profile_endpoint(
    name: str,
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> dict[str, Any]:
    profiles = discover_profiles(
        config_dir=state.engine.config.config_dir / "profiles",
        repo_dir=Path(__file__).resolve().parents[2] / "profiles",
    )
    if name not in profiles:
        raise HTTPException(status_code=404, detail="profile not found")
    path, profile = profiles[name]
    payload = profile.model_dump(mode="json")
    payload["_source_path"] = str(path)
    return payload


_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@router.post(
    "/profiles", response_model=ProfileSummary, status_code=status.HTTP_201_CREATED
)
def upload_profile_endpoint(
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
    body: PipelineProfile | PromptProfile,
) -> ProfileSummary:
    """Persist a user-supplied profile under ``{config_dir}/profiles/``.

    The accepted body is a validated profile model — invalid YAML never
    reaches the disk. The profile name is restricted to kebab-case
    (lowercase, digits, ``-``, ``_``; 1–64 chars) so it can't escape
    the profiles directory through path-traversal segments like
    ``../../etc/passwd``.
    """
    del token  # uses default config_dir per process namespace
    if not _PROFILE_NAME_RE.match(body.name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid profile name {body.name!r}: must match "
                f"{_PROFILE_NAME_RE.pattern}"
            ),
        )
    profiles_dir = state.engine.config.config_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    target = (profiles_dir / f"{body.name}.yaml").resolve()
    # Defense in depth — the regex above already forbids slashes, but
    # we still confirm the resolved path stays inside profiles_dir
    # before writing.
    if not target.is_relative_to(profiles_dir.resolve()):
        raise HTTPException(
            status_code=400,
            detail="profile name resolves outside the profiles directory",
        )
    import yaml as _yaml

    target.write_text(
        _yaml.safe_dump(body.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return ProfileSummary(
        name=body.name,
        kind=body.kind,
        description=body.description,
        path=str(target),
    )


# ─────────────────────────────────────────────────────────────────
# /operations + /backends — discovery surface
# ─────────────────────────────────────────────────────────────────


@router.get("/operations", response_model=list[OperationSummary])
def list_operations_endpoint(
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> list[OperationSummary]:
    return [_op_summary(op) for op in OpRegistry.list_all()]


@router.get("/operations/{name}", response_model=OperationDetail)
def get_operation_endpoint(
    name: str,
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> OperationDetail:
    if not OpRegistry.has(name):
        raise HTTPException(status_code=404, detail="op not found")
    op = OpRegistry.get(name)
    return OperationDetail(
        **_op_summary(op).model_dump(),
        description=(op.__doc__ or "").strip(),
        params_schema=op.params_model.model_json_schema(),
        declared_resources=list(op.declared_resources),
        backends=BackendRegistry.for_op(op.name),
    )


@router.get("/backends", response_model=list[BackendSummary])
def list_backends_endpoint(
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> list[BackendSummary]:
    return [
        BackendSummary(op_name=b.op_name, name=b.name, version=b.version)
        for b in BackendRegistry.list_all()
    ]


@router.get("/backends/{name}", response_model=BackendDetail)
def get_backend_endpoint(
    name: str,
    _: Annotated[ApiTokenInfo, Depends(require_token)],
    op: Annotated[str | None, Query()] = None,
) -> BackendDetail:
    candidates = [b for b in BackendRegistry.list_all() if b.name == name]
    if op is not None:
        candidates = [b for b in candidates if b.op_name == op]
    if not candidates:
        raise HTTPException(status_code=404, detail="backend not found")
    if len(candidates) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"backend name {name!r} is registered for multiple ops; "
                f"disambiguate with ?op="
            ),
        )
    backend = candidates[0]
    return BackendDetail(
        op_name=backend.op_name,
        name=backend.name,
        version=backend.version,
        requires=backend.requires.model_dump(),
        health=backend.health(),
    )


# ─────────────────────────────────────────────────────────────────
# /tokens — admin (token-gated; the first token is bootstrapped by CLI)
# ─────────────────────────────────────────────────────────────────


@router.post(
    "/tokens",
    response_model=TokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_token_endpoint(
    body: TokenCreateRequest,
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> TokenCreateResponse:
    secret = create_token(state.cache, label=body.label, namespace=body.namespace)
    return TokenCreateResponse(
        token_id=secret.token_id,
        label=secret.label,
        namespace=secret.namespace,
        secret=secret.secret,
    )


@router.get("/tokens", response_model=list[ApiTokenInfo])
def list_tokens_endpoint(
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
    include_revoked: bool = False,
) -> list[ApiTokenInfo]:
    return list_tokens(state.cache, include_revoked=include_revoked)


@router.delete("/tokens/{token_id}")
def revoke_token_endpoint(
    token_id: str,
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> JSONResponse:
    if not revoke_token(state.cache, token_id):
        raise HTTPException(status_code=404, detail="token not found")
    return JSONResponse({"token_id": token_id, "revoked": True})


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _op_summary(op: Any) -> OperationSummary:
    return OperationSummary(
        name=op.name,
        version=op.version,
        input_kinds=[k.value for k in op.input_kinds],
        output_kinds=[k.value for k in op.output_kinds],
        default_backend=op.default_backend,
        variadic_inputs=bool(op.variadic_inputs),
    )
