#!/usr/bin/env bash
set -euo pipefail

task=$(python - "$GITHUB_WORKSPACE/cloud/ple-start.json" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(config["task"])
PY
)
if [[ ! "$task" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo 'Invalid task ID in cloud/ple-start.json.' >&2
  exit 1
fi

python "$GITHUB_WORKSPACE/cloud/live_progress.py" --output-dir "$PLE_OPERATOR" &
progress_pid=$!
trap 'kill "$progress_pid" 2>/dev/null || true; wait "$progress_pid" 2>/dev/null || true' EXIT

cd "$PLE_KIT"
uv run python eval/run.py "$GITHUB_WORKSPACE" \
  --env-file "$PLE_OPERATOR/model.env" \
  --tasks "$task" --trials 1 --attempts 1 --out "$PLE_RUN" \
  2>&1 | tee "$PLE_OPERATOR/evaluation.log"
