# `analysis-full` — bundled starter pipeline

This profile is the engine's reference end-to-end content-analysis flow.
It exists under `profiles/analysis-full/` in the repo and is discovered
automatically by `med profile ls`.

## Pipeline

```
source (Video)
   └─ video.extract_audio        → Audio
       └─ audio.transcribe_diarized → Transcript (speaker_id per segment)
           └─ speakers.identify   → Transcript (+ resolved speaker_name)
               └─ intelligence.analyze → SessionAnalysis (windowed JSON)
                   └─ report.session   → MarkdownArtifact
```

Five files drive the behavior:

| File | What it does |
| ---- | ------------ |
| `analysis-full.yaml`        | DAG: which ops, which params, which inputs/outputs |
| `analyze_prompt.md`         | System prompt for the LLM (resolved into `intelligence.analyze`'s `prompt` field at validation time) |
| `analysis_schema.json`      | JSON schema the LLM must produce per analysis window |
| `speakers.csv`              | Name database `speakers.identify` matches against |
| `session_report.md.j2`      | Jinja2 template `report.session` renders into the final Markdown |
| `zeitgeist_report.md.j2`    | Jinja2 template `report.zeitgeist` renders when aggregating across sessions |

## Running it

```bash
# Local-first (Apple Silicon, no API keys, reproducible):
uv sync --extra llm-mlx
uv run med profile run analysis-full --input <video-artifact-id>

# Cloud LLM:
export GEMINI_API_KEY=...
uv run med profile run analysis-full --input <video-id> \
    --param analyzed.model=gemini-2.5-flash \
    --param analyzed.backend=gemini
```

Paths inside the YAML are currently resolved relative to your current
working directory, so run from the repo root (or copy the profile into
`~/.config/media_engine/profiles/` and adjust paths to absolute).

## Customizing

- **Different LLM:** edit the `analyzed.params.model` field. Setting it to
  a `mlx-community/...` model selects the `mlx-lm` backend; setting it to
  `gemini-...` selects `gemini`; `claude-...` selects `claude`. Prefix
  routing lives in `intelligence/extract.py`.
- **Different schema:** edit `analysis_schema.json`. The engine validates
  the LLM's output per window against this schema. Stick to the strict
  subset documented in `runtime/jsonschema.py` — `type`, `properties`,
  `required`, `additionalProperties`, `items`, `enum`, `minItems`/
  `maxItems`, `minLength`/`maxLength`. Anything else is silently
  ignored.
- **Different prompt:** edit `analyze_prompt.md`. The cache key tracks
  the *resolved text* (not the file path), so editing the prompt
  invalidates cached `intelligence.analyze` runs on the next invocation.
- **Different speakers:** edit `speakers.csv`. Same cache-on-content trick
  applies via `speakers.identify`'s `speaker_db_sha` field.
- **Different report layout:** edit `session_report.md.j2`. The template
  context exposes `session`, `segments`, `model`, `backend`,
  `speaker_names`, plus anything you pass through `extra_context`.

## Template context — `report.session`

| Variable        | Type                          | Description |
| --------------- | ----------------------------- | ----------- |
| `session`       | `SessionAnalysis`             | the full input artifact |
| `segments`      | `list[dict]`                  | per-window analysis records (`window_index`, `start`, `end`, `analysis`, `speaker`) |
| `model`         | `str \| None`                 | model id used by `intelligence.analyze` |
| `backend`       | `str \| None`                 | backend name resolved at run time |
| `speaker_names` | `dict[cluster_id, name\|None]`| from `speakers.identify` |
| `title`         | `str \| None`                 | from the op params; falls back to a default |
| `extra_context` | `dict[str, Any]`              | merged into the top-level template namespace |

## Template context — `report.zeitgeist`

| Variable    | Type                        | Description |
| ----------- | --------------------------- | ----------- |
| `sessions`  | `list[SessionAnalysis]`     | every input artifact |
| `aggregate` | `dict`                      | precomputed counters: `avg_polarity`, `polarity_count`, `top_topics`, `top_entities`, `top_claims`, `top_speakers`, `n_sessions`, `n_windows` |
| `title`     | `str \| None`               | from op params |

## Why generic dimensions?

The engine deliberately holds zero domain opinions (engine principle 5).
The bundled profile mirrors that by picking content-neutral fields —
`summary / topics / entities / claims / sentiment{polarity,confidence}
/ questions` — that work for any spoken-video corpus (lectures, podcasts,
news, hardware tutorials, recipes, …). Specialize by cloning the
profile directory and rewriting the schema + prompt — no engine changes
required.
