"""Host-authorized, sequential integration after all producers have accepted."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from feasibility_scheduler.models import Task

INTEGRATION_ID = "__contract_integration__"


def integration_task(tasks, plan, editable_files):
    if any(t.id == INTEGRATION_ID for t in tasks):
        raise ValueError("reserved contract integration task ID")
    owned = {p for t in tasks for p in t.payload.get("owned_files", ())}
    references = {p for t in tasks for p in t.payload.get("reference_files", {})}
    if (not isinstance(editable_files, (list, tuple)) or not editable_files
            or any(not isinstance(p, str) or not p or "\\" in p or ":" in p
                   or PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts
                   or str(PurePosixPath(p)) != p for p in editable_files)
            or len(set(editable_files)) != len(editable_files)
            or not set(editable_files) <= owned or set(editable_files) & references):
        raise ValueError("integration_files must be unique canonical paths within selected output ownership")
    return Task(id=INTEGRATION_ID, dependencies=tuple(t.id for t in tasks),
                constraints_declared=True, output_contract={"type": "object", "required": ["ok", "files"]},
                payload={"contract_integration": True, "contract_plan": plan,
                         # Sequential edit permission; do not claim a second producer ownership.
                         "owned_files": [], "integration_files": sorted(editable_files),
                         "integration_read_paths": sorted(owned | references)})


def integration_context(task, workspace):
    root = Path(workspace).resolve()
    artifacts, hashes = {}, {}
    for path in task.payload["integration_read_paths"]:
        target = (root / path).resolve()
        target.relative_to(root)
        content = target.read_bytes()  # Missing producer output is an error, never invented context.
        artifacts[path] = content.decode("utf-8")
        hashes[path] = hashlib.sha256(content).hexdigest()
    editable = set(task.payload["integration_files"])
    context = {"editable_artifacts": {p: artifacts[p] for p in sorted(editable)},
               "read_only_artifacts": {p: v for p, v in artifacts.items() if p not in editable},
               "sha256": hashes, "immutable_contract": task.payload["contract_plan"]}
    prompt = ("Integrate the accepted producer outputs using their actual interfaces. "
              "Connect entry points to existing modules; preserve their behavior and do not duplicate "
              "read-only implementations. Only edit editable_artifacts; never return read_only_artifacts. "
              "Preserve all immutable requirements. Independent root checks run after delivery. "
              'Return only {"ok":true,"files":{"each editable path":"complete content"}}.\n'
              + json.dumps(context, ensure_ascii=False))
    return context, prompt
