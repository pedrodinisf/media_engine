"""Static introspection of profile nodes → models + provider + requirement hints.

Powers the Web UI's "what does this profile actually use?" surfaces:

  * per-node model / provider chips + requirement hints in the profile
    workspace (via the enriched ``POST /profiles/validate`` response), and
  * a compact per-profile digest on the Profiles list cards (via ``GET
    /profiles``).

Pure static analysis over ``OpRegistry`` + ``BackendRegistry`` + the params
JSON Schema + the doctor's live requirement checks — no artifacts, no
execution — so it's safe to run on every keystroke of the live validator and
for every card on the list page.

The model-id → provider prefix map here mirrors the op routers
(``_backend_for_model``) and the client's ``classifyModelProvider`` (in
``web/src/lib/components/forms/schema.ts``); keep the three in sync.
"""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from media_engine.backends._base import Backend, BackendRegistry
from media_engine.ops import Operation, OpRegistry
from media_engine.profiles.schema import PipelineProfile, Profile
from media_engine.runtime.doctor import (
    BackendDoctorReport,
    Provider,
    check_backend,
    classify_provider,
)

# A field names a model when it's exactly ``model`` or ends in ``_model``
# (vlm_model, synth_model, transcribe_model, diarize_model). Deliberately NOT
# "has an enum" — ``style`` / ``output_kind`` / ``media_resolution`` carry
# enums too but aren't models.
_MODEL_FIELD_RE = re.compile(r"(^|_)model$")

# Model-id prefix → provider. Local weights live under a namespace prefix;
# cloud ids are dash-prefixed vendor families.
_LOCAL_PREFIXES = ("mlx-community/", "sentence-transformers/", "pyannote/", "BAAI/")
_CLOUD_PREFIXES = ("gemini-", "claude-", "gpt-")

# Cloud model family → the env var that unblocks it (for composite hints).
_CLOUD_KEY_FOR_PREFIX = {
    "gemini-": "GEMINI_API_KEY",
    "claude-": "ANTHROPIC_API_KEY",
    "gpt-": "OPENAI_API_KEY",
}


def classify_model_provider(model_id: str) -> Provider:
    """Classify a model id as cloud vs local by its prefix (see module doc)."""
    for prefix in _LOCAL_PREFIXES:
        if model_id.startswith(prefix):
            return "local"
    for prefix in _CLOUD_PREFIXES:
        if model_id.startswith(prefix):
            return "cloud"
    return "unknown"


def model_param_fields(params_model: type[BaseModel]) -> list[str]:
    """Field names on a params model that name a model (``model`` / ``*_model``)."""
    return [name for name in params_model.model_fields if _MODEL_FIELD_RE.search(name)]


def _hint_from_backend_report(rep: BackendDoctorReport) -> str | None:
    """Human 'needs X' nudge from the first non-ok requirement of a backend."""
    for check in rep.requirements:
        if check.status == "ok":
            continue
        if check.kind == "env":
            return f"needs {check.name}"
        if check.kind in ("service", "binary"):
            return f"install {check.name}"
        if check.kind == "hardware":
            return f"needs {check.name}"
        if check.kind == "memory":
            return f"needs {check.name}"
    return None


def _reachable_backends(
    op_cls: type[Operation],
    _visited: frozenset[str] | None = None,
) -> list[type[Backend]]:
    """This op's backends plus (recursively) its delegates' backends."""
    visited = _visited or frozenset()
    if op_cls.name in visited:
        return []
    visited = visited | {op_cls.name}
    out: list[type[Backend]] = [
        BackendRegistry.get(op_cls.name, name) for name in BackendRegistry.for_op(op_cls.name)
    ]
    for delegate in op_cls.delegates_to:
        if OpRegistry.has(delegate):
            out.extend(_reachable_backends(OpRegistry.get(delegate), visited))
    return out


class _HintCache:
    """Per-request memo for backend doctor reports.

    Backend checks probe the live env (``os.environ``, ``shutil.which``,
    ``importlib``), which a mid-session ``PUT /settings/secrets`` can change —
    so this is scoped to one request/call, never cached globally.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], BackendDoctorReport] = {}

    def report(self, backend_cls: type[Backend]) -> BackendDoctorReport:
        key = (backend_cls.op_name, backend_cls.name)
        cached = self._cache.get(key)
        if cached is None:
            cached = check_backend(backend_cls)
            self._cache[key] = cached
        return cached


def enrich_node(
    op_name: str,
    params: dict[str, Any],
    backend: str | None,
    cache: _HintCache | None = None,
) -> dict[str, Any]:
    """Static per-node summary: resolved backend, provider, models, hint.

    Degrades gracefully — an unknown op or half-typed params yields a
    ``provider="unknown"`` stub instead of raising, so the live validator
    keeps returning 200 mid-edit."""
    cache = cache or _HintCache()
    result: dict[str, Any] = {
        "resolved_backend": None,
        "provider": "unknown",
        "models": [],
        "requirement_hint": None,
    }
    if not OpRegistry.has(op_name):
        return result
    op_cls = OpRegistry.get(op_name)

    # Try to build a params model (needed for router select_backend), but
    # tolerate a half-typed / partial node mid-edit — model fields are still
    # extracted from params-or-default so the chips don't vanish while typing.
    try:
        pm: BaseModel | None = op_cls.params_model(**params)
    except ValidationError:
        pm = None

    # Model fields + their (possibly default) values, each tagged by provider.
    models: list[dict[str, Any]] = []
    for fname in model_param_fields(op_cls.params_model):
        if pm is not None:
            raw = getattr(pm, fname, None)
        elif fname in params:
            raw = params[fname]
        else:
            fld = op_cls.params_model.model_fields[fname]
            raw = None if fld.is_required() else fld.default
        value = str(raw) if raw is not None else None
        models.append(
            {
                "name": fname,
                "value": value,
                "provider": classify_model_provider(value) if value else "unknown",
            }
        )
    result["models"] = models

    # Resolved backend — precedence mirrors Engine._resolve_backend minus the
    # B-008 cross-check (that's the validate/preflight's job, not this hint).
    routed = op_cls().select_backend(pm) if pm is not None else None
    resolved = backend or routed or op_cls.default_backend
    result["resolved_backend"] = resolved

    if resolved and BackendRegistry.has(op_name, resolved):
        backend_cls = BackendRegistry.get(op_name, resolved)
        result["provider"] = classify_provider(backend_cls.requires)
        rep = cache.report(backend_cls)
        if rep.overall != "ok":
            result["requirement_hint"] = _hint_from_backend_report(rep)
    elif op_cls.delegates_to:
        # Embedded composite (e.g. video.comprehend) — no single backend.
        # Provider reads "composite"; the per-model providers carry the
        # cloud/local detail. Hint = first blocking requirement across the
        # reachable backends, but only when a cloud key it needs is actually
        # absent / a local dep is missing.
        result["provider"] = "composite"
        result["requirement_hint"] = _composite_hint(op_cls, models)
    return result


def _composite_hint(
    op_cls: type[Operation],
    models: list[dict[str, Any]],
) -> str | None:
    """Requirement hint for an embedded composite.

    Driven by the models the profile actually configures, so an all-local
    composite never reports an API key it won't use:

    1. A cloud model whose API key is unset → ``needs GEMINI_API_KEY`` (the
       common failure, e.g. ``synth_model=gemini-2.5-pro`` with no key).
    2. Else a reachable local backend gated by a **non**-``_API_KEY`` env that's
       unset (e.g. ``HF_TOKEN`` for pyannote). We deliberately skip ``_API_KEY``
       envs here — those belong to *alternate* cloud backends a locally-routed
       composite won't touch, so surfacing them would be a false
       'needs ANTHROPIC_API_KEY' on an all-local profile."""
    for model in models:
        if model["provider"] != "cloud" or not model["value"]:
            continue
        for prefix, key in _CLOUD_KEY_FOR_PREFIX.items():
            if str(model["value"]).startswith(prefix) and not os.environ.get(key):
                return f"needs {key}"
    for backend_cls in _reachable_backends(op_cls):
        for env_name in backend_cls.requires.env:
            if not env_name.endswith("_API_KEY") and not os.environ.get(env_name):
                return f"needs {env_name}"
    return None


def _profile_nodes(
    profile: Profile,
) -> list[tuple[str, dict[str, Any], str | None]]:
    """``(op, params, backend)`` per node, in ``compiled_nodes`` order.

    Pipeline → graph order; prompt → the single ``run`` node on ``default_op``."""
    if isinstance(profile, PipelineProfile):
        return [(n.op, dict(n.params), n.backend) for n in profile.graph]
    # PromptProfile — a single op call on default_op.
    return [(profile.default_op, {}, profile.default_backend)]


def enrich_profile_nodes(profile: Profile) -> list[dict[str, Any]]:
    """Per-node enrichment aligned 1:1 with ``validate_profile_structure``'s
    ``compiled_nodes`` (graph order for pipelines; the single ``run`` node for
    prompt profiles). One requirement-hint cache is shared across the profile."""
    cache = _HintCache()
    return [
        enrich_node(op_name, params, backend, cache)
        for op_name, params, backend in _profile_nodes(profile)
    ]


def profile_digest(profile: Profile) -> dict[str, Any]:
    """Compact per-profile summary for the Profiles list cards.

    Returns distinct models (id + provider), distinct providers, and the
    aggregate requirement hints across every node. Prompt profiles are treated
    as a single node on their ``default_op``."""
    cache = _HintCache()
    models: dict[str, str] = {}  # model id → provider
    providers: list[str] = []
    hints: list[str] = []
    for op_name, params, backend in _profile_nodes(profile):
        enriched = enrich_node(op_name, params, backend, cache)
        for m in enriched["models"]:
            if m["value"]:
                models[m["value"]] = m["provider"]
        prov = enriched["provider"]
        if prov not in providers and prov != "unknown":
            providers.append(prov)
        hint = enriched["requirement_hint"]
        if hint and hint not in hints:
            hints.append(hint)

    return {
        "models": [{"name": v, "provider": p} for v, p in models.items()],
        "providers": providers,
        "requirement_hints": hints,
    }


__all__ = [
    "classify_model_provider",
    "enrich_node",
    "enrich_profile_nodes",
    "model_param_fields",
    "profile_digest",
]
