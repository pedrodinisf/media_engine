# Writing a Profile

A profile is a **named, parameterized pipeline** stored as data — YAML for
explicit DAGs, markdown-with-frontmatter for prompt-driven shorthand. The
engine compiles a profile to a runtime `Pipeline` and dispatches it through
the DAG executor with caching, lineage, semaphores, and retry — the same
machinery `Engine.run` uses for individual ops.

## Where profiles live

The loader searches three places (later directories override earlier ones):

1. `~/.config/media_engine/profiles/` — your personal profiles
2. `<engine repo>/profiles/` — bundled starter profiles
3. Any directory passed via `--profile-dir`

`med profile ls` shows what was discovered.

## Two flavors

### Pipeline profile (YAML)

For explicit multi-op DAGs:

```yaml
profile_schema_version: "1.0"
name: davos-full
kind: pipeline
description: WEF talk → speaker-diarized intelligence + report

inputs:
  - { name: source, kind: video }

graph:
  - id: audio
    op: video.extract_audio
    inputs: { in: source }

  - id: transcript
    op: audio.transcribe_diarized
    inputs: { audio: audio }
    params:
      language: en
      transcribe_model: mlx-community/whisper-large-v3-mlx

  - id: chunks
    op: chunk.semantic
    inputs: { transcript: transcript }
    params: { max_chars: 2000, overlap_chars: 200 }

  - id: embeddings
    op: embed.text
    inputs: { chunks: chunks }

outputs: [transcript, embeddings]
```

Field reference:

| Field                      | Purpose                                                         |
| -------------------------- | --------------------------------------------------------------- |
| `profile_schema_version`   | Always `"1.0"` for now. Lets us migrate without breaking files. |
| `name`                     | Used by `med profile run NAME`. Must be unique.                 |
| `kind`                     | `pipeline` for this flavor.                                     |
| `description`              | Shown in `med profile ls`.                                      |
| `inputs[]`                 | Source artifacts the caller supplies at run time. Each has a    |
|                            | `name` (referenced from `graph[].inputs`) and a `kind` (lowercase|
|                            | Kind value: `video`, `audio`, `image`, …).                      |
| `graph[]`                  | List of op invocations.                                         |
| `graph[].id`               | Node id (must be unique within the profile).                    |
| `graph[].op`               | A registered op name (`med ops` to list).                       |
| `graph[].inputs`           | Mapping of op-input label → source name OR upstream node id.    |
|                            | Order matters; the values become the op's positional inputs.    |
|                            | List form (`[source]`) also accepted.                           |
| `graph[].params`           | Op-specific params (validated against the op's `params_model`). |
| `graph[].backend`          | Optional backend selector (must be registered for the op).      |
| `graph[].depends_on`       | Extra explicit dependencies (failure cascades through these).   |
| `outputs`                  | Node ids whose outputs are returned. Defaults to leaf nodes.    |

### Prompt profile (markdown)

For VLM/LLM single-op shorthand:

```markdown
---
profile_schema_version: "1.0"
name: technical-academic
kind: prompt
default_op: video.multimodal
default_backend: gemini
schema_path: profiles/davos/analysis_schema.json   # optional JSON schema
---
You are an expert technical reviewer. Extract:
- Major arguments
- Supporting evidence
- Open questions

Output as Markdown headings. Be concise.
```

The markdown body becomes the `system_prompt` parameter to `default_op`.
The frontmatter optionally pins a default backend and an output JSON schema
for structured extraction.

## Run a profile

```bash
# 1. ingest a source artifact
med acquire path/to/talk.mp4
# → 79e6b42c2b2b...

# 2. run the profile, mapping the source name to the artifact id
med profile run davos-full --input source=79e6b42c

# Output (one line per produced artifact):
# transcript    a1b2c3d4...
# embeddings    e5f6a7b8...
```

Multiple sources? Repeat `--input`:

```bash
med profile run compare-talks \
    --input source_a=79e6b42c \
    --input source_b=8f9d3a1c
```

## Compile-time validation

Before dispatch, the loader + compiler check:

- Op exists in `OpRegistry`.
- Backend (when set) is registered for that op.
- Every declared `input` is supplied by the caller.
- `graph` has no cycles, no duplicate node ids, no unresolved refs.
- Profile schema validates (`profile_schema_version`, required fields).

Errors are surfaced before the engine starts running anything.

## Run-time semantics

Once compiled, the profile is a `runtime.dag.Pipeline`. The DAG executor:

- Runs ready nodes concurrently inside `asyncio.TaskGroup`.
- Acquires per-resource semaphores (`apple_neural_engine`, `apple_gpu`,
  `cloud_concurrent`) so e.g. only one VLM runs at a time on Apple Silicon.
- Caches every node's output by `(op, version, backend, params, input_ids)`.
  Re-running the same profile against the same inputs is a series of cache
  hits.
- Retries each node per its `RetryPolicy` (cloud-shaped backends default to
  3 attempts exponential; local default 1).
- On per-node failure, downstream nodes are marked `FailedDependency`;
  independent siblings keep running. The result is partial-completion.

## Tips

- **Cheap to iterate**: change a param → just the affected node + downstream
  re-runs. Upstream cache hits keep the loop fast.
- **One profile, many backends**: pin `backend: gemini` for the cloud
  variant; copy + change to `backend: vllm-mlx` for local. The cache keeps
  both lineages independent.
- **Composite ops vs profiles**: for 2-step recurring patterns (transcribe
  + diarize), an Op composite is cleaner. For 5-step domain pipelines, a
  profile is data and easy to edit.
- **Bundled starters**: `profiles/examples/transcribe-and-diarize.yaml`
  is the smallest end-to-end shape worth copying.

## Running profiles over REST

Phase 4 added `POST /pipelines` for submitting profiles over the REST
surface. Two body shapes:

```jsonc
// Server-known profile (must appear in `med profile ls` on the API host):
{
  "profile_name": "davos-full",
  "sources": [{"name": "source", "artifact_id": "79e6b42c2b2b..."}]
}

// Inline profile (the YAML lives in the request, the server parses
// it through the same loader):
{
  "pipeline_yaml": "name: ad-hoc\nkind: pipeline\ngraph: [...]\n",
  "sources": [{"name": "source", "artifact_id": "..."}]
}
```

Either form returns `202 { "job_id": "..." }`. Subscribe to
`GET /jobs/{id}/events` (SSE) for live progress; poll
`GET /jobs/{id}` for final status. Sources must already be in the
cache (use `POST /run` with `acquire.upload` / `acquire.url` first).

## Uploading a profile

`POST /profiles` validates and persists a profile under
`{config_dir}/profiles/` so `GET /profiles` and subsequent
`POST /pipelines` calls can use it by name. The body is the same
schema documented above; `kind: pipeline` and `kind: prompt` are both
accepted.
