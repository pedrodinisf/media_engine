#!/usr/bin/env bash
# End-to-end smoke of the bundled `analysis-full` profile.
#
# Ingests a short public-domain video via `med acquire-url`, then runs
# the full analysis-full pipeline against it and prints the Markdown
# report's path + first 40 lines. Designed to be safe to run on a fresh
# checkout — degrades to a "skipped: $REASON" message when a required
# binary or API key is missing rather than failing the script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# A short public-domain video. Override via $SAMPLE_URL if you have one
# you prefer (a 1-3 minute clip is ideal).
SAMPLE_URL="${SAMPLE_URL:-https://archive.org/download/JosephBidenJanuary202021AddressToTheNation/Joseph%20Biden%20-%20January%2020%2C%202021%20-%20Address%20to%20the%20Nation.mp4}"

skip() {
  echo "examples/analysis_full_pipeline_e2e.sh — skipped: $1"
  exit 0
}

command -v uv >/dev/null 2>&1 || skip "uv not on PATH"

# Pick the LLM backend: prefer local mlx-lm on Apple Silicon, else fall
# back to Gemini if the key is set, else skip.
LLM_FLAGS=()
if uv pip show mlx-lm >/dev/null 2>&1; then
  echo ">> using local mlx-lm backend"
elif [ -n "${GEMINI_API_KEY:-}" ]; then
  echo ">> using gemini backend (GEMINI_API_KEY set)"
  LLM_FLAGS=(--param analyzed.model=gemini-2.5-flash --param analyzed.backend=gemini)
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo ">> using claude backend (ANTHROPIC_API_KEY set)"
  LLM_FLAGS=(--param analyzed.model=claude-3-5-sonnet-20241022 --param analyzed.backend=claude)
else
  skip "no LLM available — install mlx-lm (\`uv sync --extra llm-mlx\`) or set GEMINI_API_KEY / ANTHROPIC_API_KEY"
fi

uv run med --help >/dev/null 2>&1 || skip "\`uv run med\` failed (run \`uv sync\` first)"

echo ">> ingesting sample video..."
VIDEO_ID="$(uv run med acquire-url "$SAMPLE_URL" --quality 360p --json 2>/dev/null | python -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id") or d.get("artifact_id"))' 2>/dev/null || true)"
if [ -z "$VIDEO_ID" ]; then
  skip "acquire-url failed (network? yt-dlp missing?) — set SAMPLE_URL to a local path or different URL"
fi
echo "   acquired: $VIDEO_ID"

echo ">> running analysis-full profile..."
REPORT_ID="$(uv run med profile run analysis-full --input "$VIDEO_ID" --json "${LLM_FLAGS[@]}" 2>/dev/null | python -c 'import json,sys; d=json.load(sys.stdin); print((d.get("outputs") or [{}])[0].get("id") or d.get("id"))' 2>/dev/null || true)"
if [ -z "$REPORT_ID" ]; then
  echo "!! profile run did not return a report id — inspect manually:"
  echo "   uv run med profile run analysis-full --input $VIDEO_ID"
  exit 1
fi
echo "   report: $REPORT_ID"

REPORT_PATH="$(uv run med show "$REPORT_ID" --json 2>/dev/null | python -c 'import json,sys; print(json.load(sys.stdin).get("path", ""))' 2>/dev/null || true)"
if [ -z "$REPORT_PATH" ] || [ ! -f "$REPORT_PATH" ]; then
  echo "!! could not resolve report path; got: $REPORT_PATH"
  exit 1
fi

echo ""
echo "===== $REPORT_PATH (first 40 lines) ====="
head -40 "$REPORT_PATH"
echo "===================================="
echo ""
echo "Full report: $REPORT_PATH"
echo "Re-render with a different template / title via \`med run report.session\`."
