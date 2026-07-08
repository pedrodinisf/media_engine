"""Every REST endpoint, one module so the surface is greppable.

The route handlers are thin: they auth, marshal request bodies, defer
to the engine / cache / jobs module, and shape the response. Anything
heavier — selection, schema validation, op execution — happens behind
``Engine`` so the CLI/daemon/MCP paths stay in sync with REST.
"""

from __future__ import annotations

import asyncio
import os
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
    load_profile_from_string,
)
from media_engine.profiles.pipeline import (
    ProfileCompileError,
    compile_profile,
    validate_profile_structure,
)
from media_engine.profiles.schema import PipelineProfile, PromptProfile
from media_engine.runtime.cache import ApiTokenInfo, Job
from media_engine.runtime.lineage import OperationRunRef
from media_engine.runtime.search_query import embed_query_string

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

    ``embedded`` is set when the op has no Backend layer at all
    (composite / fan-out ops like ``intelligence.summarize`` or
    ``audio.transcribe_diarized``). The UI uses this flag to render
    "(composite)" instead of "—" in the cost-preview backend field,
    which is the B-005 fix.
    """

    op: str
    backend: str | None
    embedded: bool = False
    estimate_seconds_local: float
    estimate_cost_cents: float
    estimate_tokens_in: int
    estimate_tokens_out: int


class SearchRequest(BaseModel):
    """``POST /search`` body — sync catalog query.

    ``top_k`` is bounded at 200 to keep the synchronous handler from
    starving the event loop (plan §13 risk #6); ``query`` is required
    for every mode (semantic queries the string after embedding it,
    fulltext consumes the string directly, hybrid uses both).
    """

    mode: Literal["fulltext", "semantic", "hybrid"]
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=200)
    kind: str | None = None
    refresh: bool = False


class SearchResultItem(BaseModel):
    artifact_id: str
    kind: str | None = None
    score: float
    snippet: str | None = None


class SearchResponse(BaseModel):
    mode: str
    query: str
    top_k: int
    results: list[SearchResultItem]


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
    """Discovery row for a profile.

    ``source`` lets the Web UI distinguish bundled (read-only) from
    user-editable profiles without parsing the path heuristically.
    Bundled profiles live in ``<repo>/profiles/``; user profiles in
    ``{config_dir}/profiles/``.
    """

    name: str
    kind: Literal["pipeline", "prompt"]
    description: str = ""
    path: str
    source: Literal["bundled", "user"]


class OperationSummary(BaseModel):
    name: str
    version: str
    input_kinds: list[str]
    output_kinds: list[str]
    default_backend: str | None
    variadic_inputs: bool
    # Surfaced on the summary (not just the detail) so the Web UI's
    # Settings → Config tab can render per-op resource allocations
    # without firing N+1 detail requests. Cheap to include — the field
    # is already a tuple in the op class.
    declared_resources: list[str] = Field(
        default_factory=lambda: cast(list[str], [])
    )


class OperationDetail(OperationSummary):
    description: str
    params_schema: dict[str, Any]
    backends: list[str]


class BackendSummary(BaseModel):
    op_name: str
    name: str
    version: str


class BackendDetail(BackendSummary):
    requires: dict[str, Any]
    health: str


class SecretInfo(BaseModel):
    """Status of a known secret env-var.

    The ``value`` is never returned — only whether the env var is set and
    where the value came from (shell, file, or unset). The Web UI surfaces
    "set" / "unset" as a status icon; the user can overwrite the value by
    posting a new one via ``PUT /settings/secrets``.

    ``unblocks_direct`` lists the ops whose current default / router path
    is gated by this env var and would resolve to a working backend if
    the secret were set. ``unblocks_indirect`` lists composites that
    delegate (via ``Operation.delegates_to``) to a directly-unblocked op
    — e.g. setting GEMINI_API_KEY unblocks ``intelligence.extract``
    directly and ``intelligence.summarize`` / ``intelligence.classify``
    / ``intelligence.analyze`` indirectly. ``adds_alternate`` is for
    ops that already have a working backend on this machine — the
    secret adds a new option but the op isn't currently blocked.
    """

    name: str
    label: str
    category: str
    used_by: str
    url: str = ""
    set: bool
    source: Literal["shell", "file", "unset"]
    unblocks_direct: list[str] = Field(default_factory=lambda: cast(list[str], []))
    unblocks_indirect: list[str] = Field(default_factory=lambda: cast(list[str], []))
    adds_alternate: list[str] = Field(default_factory=lambda: cast(list[str], []))


class SecretsListResponse(BaseModel):
    items: list[SecretInfo]
    file_path: str


class SecretsUpdateRequest(BaseModel):
    # ``None`` deletes the key (UI sends None for explicit "clear"); ""
    # also deletes for symmetry with the file-format behaviour.
    updates: dict[str, str | None]


class SecretsUpdateResponse(BaseModel):
    items: list[SecretInfo]
    file_path: str
    written: list[str]


class ConfigFileView(BaseModel):
    """A read-only view of a config file.

    ``exists`` is the only "you should display this" signal; ``content``
    is empty when the file is missing or the operator hasn't created it
    yet. Secret-bearing files (``secrets.env``) are returned with values
    masked so the file viewer doesn't leak the raw keys.
    """

    path: str
    exists: bool
    content: str
    is_masked: bool = False


class ConfigFilesResponse(BaseModel):
    config_toml: ConfigFileView
    resources_yaml: ConfigFileView
    secrets_env: ConfigFileView


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
    # Phase 7 privacy: the acoustic speaker ops write/read biometric voice
    # fingerprints, so they're gated off the REST surface unless the operator
    # opts in (speaker_export_enabled). Discovery (GET /operations) still
    # lists them; only submission is blocked.
    if (
        body.op.startswith("speakers.")
        and body.op != "speakers.identify"
        and not state.engine.config.speaker_export_enabled
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{body.op!r} is disabled over REST. Acoustic speaker "
                "operations handle biometric voice data; set "
                "speaker_export_enabled = true (or "
                "MEDIA_ENGINE_SPEAKER_EXPORT_ENABLED=1) to allow them."
            ),
        )
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
    op_inst = op_cls()
    routed = op_inst.select_backend(params_model)
    backend_name = body.backend or routed or op_cls.default_backend
    # An op with no registered Backend layer (composite / fan-out) gets
    # an empty backend list — flag it so the UI can render "(composite)"
    # instead of "—" in the cost preview (B-005 p1).
    registered_backends = BackendRegistry.for_op(op_cls.name)
    is_embedded = (
        op_cls.default_backend is None and not registered_backends
    )
    # Router model/backend consistency (B-008). Mirror the engine's
    # _resolve_backend validation so the cost-preview surface warns
    # ahead of submit, not only at run time. Two failure modes:
    # (a) backend exists in the registry but conflicts with the router's
    #     model-prefix dispatch → 422 naming both candidates.
    # (b) backend is a string the registry doesn't know → 422 listing
    #     the available backends. Without this, the preview returned
    #     200 with the bogus name echoed, then submission crashed deep
    #     inside ``_resolve_backend``.
    if body.backend is not None:
        if routed is not None and routed != body.backend:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.op}: backend {body.backend!r} is incompatible "
                    f"with the current params (model routes to {routed!r}). "
                    f"Either change the model param or omit the backend "
                    f"override so the router picks {routed!r}."
                ),
            )
        if (
            not is_embedded
            and body.backend not in registered_backends
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.op}: backend {body.backend!r} is not registered. "
                    f"Available: {sorted(registered_backends) or '(none)'}."
                ),
            )

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
        embedded=is_embedded,
        estimate_seconds_local=estimate.local_seconds,
        estimate_cost_cents=estimate.cloud_cents,
        estimate_tokens_in=estimate.tokens_in,
        estimate_tokens_out=estimate.tokens_out,
    )


# ─────────────────────────────────────────────────────────────────
# /search — sync catalog query (Phase 6 commit 46)
# ─────────────────────────────────────────────────────────────────


_SEARCH_TIMEOUT_SECONDS = 30.0


@router.post("/search", response_model=SearchResponse)
async def post_search(
    body: SearchRequest,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> SearchResponse:
    """Run ``search.{fulltext,semantic,hybrid}`` synchronously.

    Unlike ``POST /run`` (which wraps the call in a job + SSE), this
    endpoint awaits the engine inline so the UI's type-as-you-go
    search box gets sub-second feedback. Semantic + hybrid embed the
    query string upstream via ``embed_query_string`` (sentence-
    transformers) before dispatching, mirroring the ``med search``
    CLI flow. A 30 s timeout guards against runaway corpora; long
    queries should use ``POST /run`` for the async/SSE path instead.
    """
    del token  # access gated; search reads honour engine namespace via Engine
    kind_filter: tuple[Kind, ...] | None = None
    if body.kind is not None:
        try:
            kind_filter = (Kind(body.kind.lower()),)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"unknown kind: {body.kind!r}"
            ) from e

    refresh_nonce: str | None = None
    if body.refresh:
        from uuid import uuid4

        refresh_nonce = uuid4().hex

    async def _do_search() -> list[AnyArtifact]:
        if body.mode == "fulltext":
            return await state.engine.run(
                "search.fulltext",
                query=body.query,
                top_k=body.top_k,
                kind_filter=kind_filter,
                refresh_nonce=refresh_nonce,
            )
        try:
            emb_id = await asyncio.to_thread(
                embed_query_string, state.engine.config, body.query
            )
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        if body.mode == "semantic":
            return await state.engine.run(
                "search.semantic",
                inputs=[emb_id],
                top_k=body.top_k,
                kind_filter=kind_filter,
                refresh_nonce=refresh_nonce,
            )
        return await state.engine.run(
            "search.hybrid",
            inputs=[emb_id],
            query=body.query,
            top_k=body.top_k,
            kind_filter=kind_filter,
            refresh_nonce=refresh_nonce,
        )

    try:
        outputs = await asyncio.wait_for(_do_search(), timeout=_SEARCH_TIMEOUT_SECONDS)
    except TimeoutError as e:
        raise HTTPException(
            status_code=504,
            detail=(
                f"search timed out after {_SEARCH_TIMEOUT_SECONDS:.0f}s; "
                f"use POST /run for batch workloads"
            ),
        ) from e

    # search.* ops emit an Analysis whose metadata['results'] is the
    # ranked hit list (architecture.md §11 deviation note).
    raw_results: list[Any] = []
    if outputs:
        raw = outputs[0].metadata.get("results")
        if isinstance(raw, list):
            raw_results = cast("list[Any]", raw)

    items: list[SearchResultItem] = []
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        row = cast("dict[str, Any]", r)
        art_id = row.get("artifact_id")
        if not isinstance(art_id, str):
            continue
        score_raw = row.get("score")
        try:
            score = float(score_raw) if score_raw is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        kind_raw = row.get("kind")
        snippet_raw = row.get("snippet")
        items.append(
            SearchResultItem(
                artifact_id=art_id,
                kind=str(kind_raw) if kind_raw is not None else None,
                score=score,
                snippet=str(snippet_raw) if snippet_raw is not None else None,
            )
        )

    return SearchResponse(
        mode=body.mode, query=body.query, top_k=body.top_k, results=items
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
    # B-001: pass cache + namespace so the pumper can replay any
    # events the job already emitted (between POST /run returning and
    # this EventSource handshake completing) before switching to live.
    return EventSourceResponse(
        job_event_stream(
            state.engine.event_bus,
            job_id,
            cache=state.cache,
            namespace=token.namespace,
        )
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
    include_ephemeral: Annotated[bool, Query()] = False,
) -> ArtifactPage:
    """Paginated artifact list, newest first.

    Offset-based pagination: pass ``?offset=<n>&limit=<m>`` to walk a
    large cache page by page. We over-fetch by one row to detect
    whether there's a next page without a separate COUNT query — if
    we get back ``limit + 1`` rows, the surplus is dropped from the
    response and the client knows to call again with
    ``offset = current_offset + limit``.

    ``include_ephemeral`` (default false) hides internal scaffolding
    artifacts — today the single-frame FrameSets that
    ``video.comprehend``'s fan-out produces, one per analysed frame.
    Set to true to debug-inspect them.
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
        include_ephemeral=include_ephemeral,
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
    user_dir = (state.engine.config.config_dir / "profiles").resolve()
    out: list[ProfileSummary] = []
    for name, (path, profile) in discover_profiles(
        config_dir=state.engine.config.config_dir / "profiles",
        repo_dir=Path(__file__).resolve().parents[2] / "profiles",
    ).items():
        resolved = path.resolve()
        is_user = (
            user_dir.exists() and resolved.is_relative_to(user_dir)
        )
        out.append(
            ProfileSummary(
                name=name,
                kind=profile.kind,
                description=profile.description,
                path=str(path),
                source="user" if is_user else "bundled",
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
        source="user",  # POST always writes to {config_dir}/profiles
    )


class ValidateProfileRequest(BaseModel):
    pipeline_yaml: str


class CompiledNodeRef(BaseModel):
    id: str
    op: str
    backend: str | None = None
    inputs: list[str] = Field(default_factory=lambda: cast(list[str], []))


class ValidateProfileResponse(BaseModel):
    """Shape of ``POST /profiles/validate``.

    Success: ``ok=True`` + ``compiled_nodes``. Failure: ``ok=False`` +
    typed error envelope. The endpoint returns ``200`` for both shapes
    so the Web UI's live-compile indicator doesn't have to special-case
    expected-failure responses — it just reads ``body.ok``.
    """

    ok: bool
    compiled_nodes: list[CompiledNodeRef] = Field(
        default_factory=lambda: cast(list[CompiledNodeRef], [])
    )
    error_class: str | None = None
    message: str | None = None
    line: int | None = None


def _extract_yaml_line(err: BaseException | None) -> int | None:
    """Pull a 1-based line number out of a PyYAML error when present.

    PyYAML's ``YAMLError`` subclasses carry ``problem_mark`` /
    ``context_mark`` with a 0-based line index. ``None`` propagates
    through (a missing ``__cause__`` just means no line hint).
    """
    if err is None:
        return None
    mark = getattr(err, "problem_mark", None) or getattr(err, "context_mark", None)
    line = getattr(mark, "line", None) if mark is not None else None
    return line + 1 if isinstance(line, int) else None


@router.post("/profiles/validate", response_model=ValidateProfileResponse)
def post_profiles_validate(
    body: ValidateProfileRequest,
    _state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> ValidateProfileResponse:
    """Compile-check a profile YAML without persisting it.

    The Web UI's profile workspace calls this every 500 ms of idle to
    surface op typos / unwired refs / cycles to the user before they
    save or run. Parses the YAML straight from memory (no tmp file +
    no workdir creation per call) so per-keystroke validation stays
    sub-millisecond on the I/O side.
    """
    del token  # auth-only; validation is namespace-agnostic
    try:
        profile = load_profile_from_string(body.pipeline_yaml or "")
    except ProfileLoadError as e:
        return ValidateProfileResponse(
            ok=False,
            error_class="ProfileLoadError",
            message=str(e),
            line=_extract_yaml_line(e.__cause__),
        )
    try:
        compiled = validate_profile_structure(profile)
    except ProfileCompileError as e:
        return ValidateProfileResponse(
            ok=False,
            error_class="ProfileCompileError",
            message=str(e),
        )
    return ValidateProfileResponse(
        ok=True,
        compiled_nodes=[CompiledNodeRef(**c) for c in compiled],
    )


@router.delete("/profiles/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile_endpoint(
    name: str,
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> None:
    """Remove a user-overrideable profile from ``{config_dir}/profiles/``.

    Refuses to delete bundled profiles (``<repo>/profiles/``) — the
    bundled set is shipped with the package and overwriting it from a
    running process would surprise the next user who upgrades. Returns
    404 when no profile by that name exists in the user's directory
    (even if a bundled profile with the same name exists — the user
    can't delete what they don't own).
    """
    del token
    if not _PROFILE_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid profile name {name!r}: must match "
                f"{_PROFILE_NAME_RE.pattern}"
            ),
        )
    user_dir = (state.engine.config.config_dir / "profiles").resolve()
    if not user_dir.exists():
        raise HTTPException(status_code=404, detail="profile not found")

    # Look for any of the three legal extensions; we only own files we
    # created (POST /profiles writes .yaml), but a user editing by hand
    # could have dropped a .yml or .md sidecar.
    target: Path | None = None
    for suffix in (".yaml", ".yml", ".md"):
        candidate = (user_dir / f"{name}{suffix}").resolve()
        if not candidate.is_relative_to(user_dir):
            # Defense in depth — the kebab regex already forbids
            # slashes, but resolve() should never escape the dir.
            raise HTTPException(
                status_code=400,
                detail="profile name resolves outside the profiles directory",
            )
        if candidate.is_file():
            target = candidate
            break
    if target is None:
        raise HTTPException(status_code=404, detail="profile not found")
    target.unlink()
    return None


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
# /settings/* — operator-managed engine surfaces
# ─────────────────────────────────────────────────────────────────


@router.get("/settings/doctor")
def get_settings_doctor(
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
    op: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    """Run the same dep-matrix walk as ``med doctor`` and return JSON.

    Re-evaluated on every call — env vars, importable packages, and
    binaries can change while the engine is up (secrets edit, brew
    install). The Web UI's Doctor tab refreshes on demand; we don't
    cache.
    """
    # Token used only to gate access; the doctor walks the registry, not
    # namespaced data.
    del state
    from media_engine.runtime.doctor import diagnose

    return diagnose(op_filter=op).to_dict()


def _compute_env_impact() -> dict[str, dict[str, list[str]]]:
    """Walk the op×backend registry and group by env-var dependency.

    Returns ``{env_name: {direct: [op], indirect: [op], alternate: [op]}}``.

    * **direct** — ops that would have a working backend *if* this env
      var were set, AND currently do not (i.e. the secret is the missing
      link). Computed by checking each backend's BackendRequirements env
      list; if the backend's only blocker is this env var, it's
      direct.
    * **indirect** — composites that declare ``delegates_to`` and
      transitively reach a directly-unblocked op.
    * **alternate** — ops where this env var would *add* a backend but
      the op already has another working backend (e.g. GEMINI for
      ``frames.analyze`` when ``vllm-mlx`` already works).
    """
    from media_engine.runtime.doctor import check_op

    impact: dict[str, dict[str, list[str]]] = {}

    op_reports = [check_op(op) for op in OpRegistry.list_all()]
    # First pass — figure out which ops currently have ≥1 working backend
    # so we can tell "direct unblock" apart from "alternate".
    op_has_working: dict[str, bool] = {}
    for r in op_reports:
        op_has_working[r.op_name] = (
            r.embedded or any(b.overall == "ok" for b in r.backends)
        )

    # Second pass — for each non-ok backend, see which single env var
    # would clear it (i.e. it's the only missing requirement).
    for r in op_reports:
        for b in r.backends:
            if b.overall == "ok":
                continue
            missing_envs = [
                req.name for req in b.requirements
                if req.kind == "env" and req.status != "ok"
            ]
            non_env_blockers = [
                req for req in b.requirements
                if req.kind != "env" and req.status != "ok"
            ]
            # The env var only "unblocks" if it's the only thing missing.
            if len(missing_envs) == 1 and not non_env_blockers:
                env = missing_envs[0]
                bucket = impact.setdefault(
                    env, {"direct": [], "indirect": [], "alternate": []}
                )
                target = "alternate" if op_has_working[r.op_name] else "direct"
                if r.op_name not in bucket[target]:
                    bucket[target].append(r.op_name)

    # Third pass — propagate through delegates_to. A composite that
    # delegates to a directly-unblocked op gets the indirect tag,
    # provided that ALL of its delegates would become reachable.
    for op_cls in OpRegistry.list_all():
        deps = op_cls.delegates_to
        if not deps:
            continue
        for bucket in impact.values():
            unblocked_set = set(bucket["direct"])
            # If every delegate is either already working or would be
            # unblocked by this env, the composite is indirectly
            # reachable.
            reachable = all(
                op_has_working.get(d, False) or d in unblocked_set
                for d in deps
            )
            touches_unblocked = any(d in unblocked_set for d in deps)
            if (
                reachable
                and touches_unblocked
                and op_cls.name not in bucket["indirect"]
                and op_cls.name not in bucket["direct"]
            ):
                bucket["indirect"].append(op_cls.name)

    return impact


def _secret_info_rows(config_dir: Path) -> tuple[list[SecretInfo], str]:
    """Build the SecretInfo rows for a given config dir.

    Combines the static KNOWN_SECRETS catalog with the actual env state
    so the UI can render "set / unset" badges. ``source`` is best-effort
    — if a key is set AND present in the file we report "file" (the
    UI's edit path wrote it); otherwise "shell".
    """
    from media_engine.runtime.secrets import (
        KNOWN_SECRETS,
        read_secrets,
        secrets_path,
    )

    file_contents = read_secrets(config_dir)
    impact = _compute_env_impact()
    rows: list[SecretInfo] = []
    for entry in KNOWN_SECRETS:
        name = entry["name"]
        env_value = os.environ.get(name)
        is_set = bool(env_value)
        if not is_set:
            source: Literal["shell", "file", "unset"] = "unset"
        elif name in file_contents and file_contents[name] == env_value:
            source = "file"
        else:
            source = "shell"
        env_impact = impact.get(name, {"direct": [], "indirect": [], "alternate": []})
        rows.append(
            SecretInfo(
                name=name,
                label=entry["label"],
                category=entry["category"],
                used_by=entry["used_by"],
                url=entry.get("url", ""),
                set=is_set,
                source=source,
                unblocks_direct=sorted(env_impact["direct"]),
                unblocks_indirect=sorted(env_impact["indirect"]),
                adds_alternate=sorted(env_impact["alternate"]),
            )
        )
    return rows, str(secrets_path(config_dir))


@router.get("/settings/secrets", response_model=SecretsListResponse)
def get_settings_secrets(
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> SecretsListResponse:
    """List the known secret env-vars + whether each is set.

    Values are never returned. The UI shows status badges and provides
    a write-only input for updates.
    """
    rows, file_path = _secret_info_rows(state.engine.config.config_dir)
    return SecretsListResponse(items=rows, file_path=file_path)


@router.put("/settings/secrets", response_model=SecretsUpdateResponse)
def put_settings_secrets(
    body: SecretsUpdateRequest,
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> SecretsUpdateResponse:
    """Persist secret env-vars to ``{config_dir}/secrets.env``.

    Also exports the new values into the running process's ``os.environ``
    (``load_secrets(override=True)``) so backends that read the env at
    call-time see them immediately. Backends that snapshot env at import
    or boot will still need a process restart — the UI shows a banner
    pointing this out.
    """
    from media_engine.runtime.secrets import load_secrets, write_secrets

    config_dir = state.engine.config.config_dir
    try:
        write_secrets(config_dir, body.updates)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None

    # Apply deletions immediately too — write_secrets removes the key
    # from the file, but the running process still has it in os.environ.
    for key, value in body.updates.items():
        if value is None or value == "":
            os.environ.pop(key, None)

    touched = load_secrets(config_dir, override=True)
    rows, file_path = _secret_info_rows(config_dir)
    return SecretsUpdateResponse(items=rows, file_path=file_path, written=touched)


_SECRET_FILE_VALUE_PATTERN = re.compile(
    r"^([A-Z_][A-Z0-9_]*)=(.*)$", re.MULTILINE
)


def _mask_secret_file(body: str) -> str:
    """Replace each KEY=VALUE pair with KEY=<set> for read-only display."""
    return _SECRET_FILE_VALUE_PATTERN.sub(
        lambda m: f"{m.group(1)}=<set>", body
    )


def _read_config_file(path: Path, *, mask: bool = False) -> ConfigFileView:
    if not path.exists():
        return ConfigFileView(path=str(path), exists=False, content="", is_masked=mask)
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as e:
        return ConfigFileView(
            path=str(path), exists=True, content=f"<read error: {e}>", is_masked=mask
        )
    if mask:
        body = _mask_secret_file(body)
    return ConfigFileView(path=str(path), exists=True, content=body, is_masked=mask)


@router.get("/settings/config-files", response_model=ConfigFilesResponse)
def get_settings_config_files(
    state: Annotated[AppState, Depends(get_state)],
    _: Annotated[ApiTokenInfo, Depends(require_token)],
) -> ConfigFilesResponse:
    """Return the three operator-facing config files for read-only display.

    Inline editing for ``config.toml`` and ``resources.yaml`` lands
    in v1.x; this endpoint just lets the Web UI show the operator
    *what's there* without making them ``cat`` the file in a shell.
    Values inside ``secrets.env`` are masked so the file viewer can't
    leak credentials.
    """
    config_dir = state.engine.config.config_dir
    from media_engine.runtime.secrets import secrets_path

    return ConfigFilesResponse(
        config_toml=_read_config_file(config_dir / "config.toml"),
        resources_yaml=_read_config_file(config_dir / "resources.yaml"),
        secrets_env=_read_config_file(secrets_path(config_dir), mask=True),
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
        declared_resources=list(op.declared_resources),
    )
