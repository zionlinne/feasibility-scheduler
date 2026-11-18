#!/usr/bin/env bash
set -euo pipefail

python "$GITHUB_WORKSPACE/cloud/live_progress.py" --output-dir "$PLE_OPERATOR" &
progress_pid=$!
trap 'kill "$progress_pid" 2>/dev/null || true; wait "$progress_pid" 2>/dev/null || true' EXIT

cd "$PLE_KIT"
uv run python eval/run.py "$GITHUB_WORKSPACE" \
  --env-file "$PLE_OPERATOR/model.env" \
  --tasks mv-conveyance-study --trials 1 --attempts 1 --out "$PLE_RUN" \
  2>&1 | tee "$PLE_OPERATOR/evaluation.log"
