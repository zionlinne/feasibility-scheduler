"""Read-only validation of current artifacts, reusable after composition repairs."""
from __future__ import annotations

import subprocess
from pathlib import Path


def workspace_file_checks(workspace: Path, paths, mode="loose", *, node_checkable=None):
    if node_checkable is None:
        node_checkable = lambda p: Path(p).suffix.lower() in {".js", ".mjs", ".cjs"}
    results = []
    root = workspace.resolve()
    for path in sorted(set(paths)):
        result = {"id": f"workspace:{path}", "path": path, "requirements": [],
                  "status": "passed", "detail": "artifact exists", "checks": [f"file_exists:{path}"]}
        results.append(result)
        try:
            target = (root / path).resolve()
            target.relative_to(root)
            if not target.is_file():
                result.update(status="failed", detail=f"落盘后找不到 {path}")
                continue
            if mode != "node-check":
                continue
            if not node_checkable(path):
                result["checks"].append(f"skip_node_check_non_js:{path}")
                continue
            result["checks"].append(f"node_check:{path}")
            process = subprocess.run(["node", "--check", str(target)], capture_output=True,
                                     text=True, timeout=60, cwd=str(root))
            if process.returncode != 0:
                error = (process.stderr or process.stdout or "").strip()[:500]
                if "ERR_UNKNOWN_FILE_EXTENSION" in error or "Unknown file extension" in error:
                    result.update(status="unverified", detail=f"VALIDATOR_ERROR: 对非 JS 文件调用了 node --check：{path}: {error}")
                else:
                    result.update(status="failed", detail=f"node --check 失败 {path}: {error}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result.update(status="unverified", detail=f"VALIDATOR_ERROR: workspace validation {path}: {exc}")
    return results
