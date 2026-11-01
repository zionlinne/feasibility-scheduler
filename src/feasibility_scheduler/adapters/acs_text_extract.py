"""ACS-proven text file extraction for artifact delivery.

Ported from api-cooperative-scheduler/scripts/run_round2_a.py::extract_files
(fence-first, then JSON artifacts). Extended only to recover *complete* entries
from a truncated JSON `files` / `artifacts` payload so a 65536-token cut does
not discard files that already closed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

# Same primary pattern as ACS A-direct (mjs/json/md), plus js/cjs used by this package.
_FENCE_RE = re.compile(
    r"(?ms)(?:^|\n)(?:#{1,3}\s*)?(?:File:\s*|路径[:：]\s*)?`?"
    r"([A-Za-z0-9_./\\-]+\.(?:mjs|js|cjs|json|md))`?\s*\n"
    r"```(?:[a-zA-Z0-9_-]*)?\n(.*?)```"
)

# Complete JSON string value: "path": "....escaped...."
_JSON_FILE_ENTRY_RE = re.compile(
    r'"(?P<path>(?:[^"\\]|\\.)+)"\s*:\s*"(?P<content>(?:[^"\\]|\\.)*)"'
)


def extract_files(answer: str) -> dict[str, str]:
    """ACS extract_files: markdown fences first, then JSON artifacts array."""
    files: dict[str, str] = {}
    if not isinstance(answer, str) or not answer:
        return files
    for m in _FENCE_RE.finditer(answer):
        path = m.group(1).replace("\\", "/").lstrip("./")
        files[path] = m.group(2)
    if files:
        return files
    start = answer.find("{")
    end = answer.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(answer[start : end + 1])
        except Exception:
            data = None
        if isinstance(data, dict):
            for a in data.get("artifacts") or []:
                if isinstance(a, dict) and isinstance(a.get("path"), str) and isinstance(a.get("content"), str):
                    files[a["path"].replace("\\", "/")] = a["content"]
            if not files and isinstance(data.get("files"), dict):
                for path, content in data["files"].items():
                    if isinstance(path, str) and isinstance(content, str):
                        files[path.replace("\\", "/")] = content
            if files:
                return files
    return files


def _unescape_json_string(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s.encode("utf-8").decode("unicode_escape")


def extract_complete_json_file_entries(answer: str) -> dict[str, str]:
    """Pull fully-closed path→content string pairs from truncated JSON text.

    Only entries whose content string terminator is present are returned; a
    trailing cut mid-string yields no entry for that path.
    """
    out: dict[str, str] = {}
    if not isinstance(answer, str) or not answer:
        return out
    # Prefer the files object region when present.
    region = answer
    marker = answer.find('"files"')
    if marker < 0:
        marker = answer.find("'files'")
    if marker >= 0:
        region = answer[marker:]
    for m in _JSON_FILE_ENTRY_RE.finditer(region):
        path = _unescape_json_string(m.group("path")).replace("\\", "/").lstrip("./")
        # Skip non-file keys
        if path in {"ok", "delivery", "notes", "manifest", "error", "type"}:
            continue
        if "/" not in path and "." not in path:
            continue
        if not re.search(r"\.(mjs|js|cjs|json|md)$", path, re.I):
            continue
        out[path] = _unescape_json_string(m.group("content"))
    # artifacts array: {"path":"...","content":"..."} complete objects
    art_re = re.compile(
        r'\{\s*"path"\s*:\s*"(?P<path>(?:[^"\\]|\\.)+)"\s*,\s*"content"\s*:\s*"(?P<content>(?:[^"\\]|\\.)*)"\s*\}'
    )
    for m in art_re.finditer(answer):
        path = _unescape_json_string(m.group("path")).replace("\\", "/").lstrip("./")
        out[path] = _unescape_json_string(m.group("content"))
    return out


def extract_delivery_map(answer: str) -> dict[str, Any]:
    """Unified extract for the Artifact Adapter.

    Returns dict with:
      files: path→content for complete files only
      method: fence | json_artifacts | json_files | json_partial | none
      extract_log: short audit notes
    """
    log: list[str] = []
    if not isinstance(answer, str):
        return {"files": {}, "method": "none", "extract_log": ["empty_or_non_string"]}
    fenced = extract_files(answer)
    if fenced:
        # extract_files may have used fence OR full JSON; detect
        if _FENCE_RE.search(answer):
            method = "fence"
        else:
            method = "json_full"
        log.append(f"{method}:{len(fenced)}")
        return {"files": fenced, "method": method, "extract_log": log}
    partial = extract_complete_json_file_entries(answer)
    if partial:
        log.append(f"json_partial:{len(partial)}")
        return {"files": partial, "method": "json_partial", "extract_log": log}
    log.append("none")
    return {"files": {}, "method": "none", "extract_log": log}
