"""Compile a ``Profile`` into a runtime ``Pipeline``.

The compiler:
1. Validates that every op referenced exists in ``OpRegistry``.
2. Validates that every backend (when set) is registered for its op.
3. Translates the YAML's named-input dict / list into ``DAGNode``'s
   ordered ``input_node_ids``.
4. Defaults the pipeline's ``outputs`` to *all* leaf nodes when the
   profile didn't list them.
5. Runs the executor's topo-sort (``validate_and_sort``) so cycles and
   unresolved refs fail at COMPILE time, not mid-run.
"""

from __future__ import annotations

from media_engine.artifacts import AnyArtifact
from media_engine.backends import BackendRegistry
from media_engine.ops import OpRegistry
from media_engine.profiles.schema import (
    GraphNodeSpec,
    PipelineProfile,
    Profile,
    PromptProfile,
)
from media_engine.runtime.dag import (
    CycleError,
    DAGNode,
    Pipeline,
    validate_and_sort,
)


class ProfileCompileError(RuntimeError):
    """Raised when a profile can't be compiled into a runnable Pipeline."""


def _validate_op_and_backend(node: GraphNodeSpec) -> None:
    try:
        op_class = OpRegistry.get(node.op)
    except LookupError as e:
        raise ProfileCompileError(
            f"node {node.id!r} references unregistered op {node.op!r}"
        ) from e
    if node.backend is not None and not BackendRegistry.has(node.op, node.backend):
        available = BackendRegistry.for_op(node.op) or ["(none)"]
        raise ProfileCompileError(
            f"node {node.id!r}: backend {node.backend!r} not registered for "
            f"{node.op!r}. Available: {', '.join(available)}"
        )
    del op_class  # unused beyond the existence check


def _input_refs(node: GraphNodeSpec) -> list[str]:
    """Return positional input refs from either dict or list form."""
    if isinstance(node.inputs, list):
        return list(node.inputs)
    # dict form: keys are op-input labels (informational only); preserve
    # insertion order of values.
    return [str(v) for v in node.inputs.values()]


def _default_outputs(graph: list[GraphNodeSpec]) -> list[str]:
    """All node ids that aren't referenced by any other node."""
    referenced: set[str] = set()
    for node in graph:
        referenced.update(_input_refs(node))
        referenced.update(node.depends_on)
    return [n.id for n in graph if n.id not in referenced]


def compile_pipeline_profile(
    profile: PipelineProfile,
    sources: dict[str, AnyArtifact],
) -> Pipeline:
    """Compile a pipeline profile + caller-supplied source artifacts."""
    declared = {s.name for s in profile.inputs}
    missing = declared - sources.keys()
    if missing:
        raise ProfileCompileError(
            f"profile {profile.name!r} declares inputs {sorted(declared)}; "
            f"missing {sorted(missing)}"
        )

    nodes: list[DAGNode] = []
    for spec in profile.graph:
        _validate_op_and_backend(spec)
        nodes.append(
            DAGNode(
                id=spec.id,
                op_name=spec.op,
                params=dict(spec.params),
                backend=spec.backend,
                input_node_ids=_input_refs(spec),
                depends_on=list(spec.depends_on),
            )
        )

    outputs = profile.outputs or _default_outputs(profile.graph)

    # Filter sources down to declared inputs (don't leak unrelated artifacts
    # into the DAG namespace).
    accepted_sources = {
        name: art for name, art in sources.items() if name in declared
    }
    pipeline = Pipeline(
        name=profile.name,
        sources=accepted_sources,
        nodes=nodes,
        outputs=outputs,
    )

    # Fail cycles / unresolved refs at COMPILE time, not deep inside the
    # executor mid-run. Reuses the executor's topo-sort so the two stay
    # consistent (a profile that compiles is guaranteed schedulable).
    try:
        validate_and_sort(pipeline)
    except (CycleError, ValueError) as e:
        raise ProfileCompileError(
            f"profile {profile.name!r}: invalid graph — {e}"
        ) from e

    return pipeline


def compile_prompt_profile(
    profile: PromptProfile,
    sources: dict[str, AnyArtifact],
) -> Pipeline:
    """Compile a prompt profile into a one-node pipeline.

    The prompt profile's body becomes the ``system_prompt`` parameter to
    its ``default_op``. Source artifacts are wired as positional inputs.
    """
    if profile.default_op not in {op.name for op in OpRegistry.list_all()}:
        raise ProfileCompileError(
            f"profile {profile.name!r} default_op {profile.default_op!r} not registered"
        )
    if not sources:
        raise ProfileCompileError(
            f"prompt profile {profile.name!r} requires at least one source artifact"
        )
    input_refs = list(sources.keys())
    params: dict[str, object] = {"system_prompt": profile.body}
    if profile.schema_path:
        params["schema"] = profile.schema_path
    return Pipeline(
        name=profile.name,
        sources=dict(sources),
        nodes=[
            DAGNode(
                id="run",
                op_name=profile.default_op,
                params=params,
                backend=profile.default_backend,
                input_node_ids=input_refs,
            ),
        ],
        outputs=["run"],
    )


def compile_profile(profile: Profile, sources: dict[str, AnyArtifact]) -> Pipeline:
    """Dispatch to the appropriate compiler for the profile flavor."""
    if isinstance(profile, PipelineProfile):
        return compile_pipeline_profile(profile, sources)
    return compile_prompt_profile(profile, sources)
