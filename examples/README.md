# Examples

Runnable scripts that show the engine end-to-end. Each example is
self-contained, prints what it's doing, and degrades gracefully (with a
`skipped: missing $X` message) when an API key or optional dep is
absent — so it's safe to run on a fresh checkout.

| Example | What it does | Prereqs |
| ------- | ------------ | ------- |
| `analysis_full_pipeline_e2e.sh` | Ingests a public-domain video, runs the bundled `analysis-full` profile against it, and prints the resulting Markdown report. | `uv sync --all-extras` for a local-only run, or `$GEMINI_API_KEY` / `$ANTHROPIC_API_KEY` for cloud LLM. |

Run from the repo root:

```bash
bash examples/analysis_full_pipeline_e2e.sh
```
