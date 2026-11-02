"""Bounded, data-only artifact checks and source requirement traceability."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from feasibility_scheduler.models import Task


def requirement_ledger(task_md: str) -> list[dict[str, str]]:
    """Keep each nonempty source line verbatim; IDs do not depend on model output."""
    return [{"id": f"R{index}", "source": line, "source_hash": hashlib.sha256(line.encode()).hexdigest()}
            for index, line in enumerate(task_md.splitlines(), 1) if line.strip()]


def check_paths(check: dict[str, Any]) -> set[str]:
    paths = {check[key] for key in ("path", "other_path") if isinstance(check.get(key), str)}
    if check.get("kind") == "external":
        paths.update(check.get("paths", []))
    return paths


def validate_checks(plan: dict[str, Any], tasks: Sequence[Task]) -> None:
    requirements = plan.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("source requirements are required")
    required = set()
    for req in requirements:
        if (not isinstance(req, dict) or not isinstance(req.get("id"), str)
                or not req["id"] or req["id"] in required
                or not isinstance(req.get("source"), str) or not req["source"].strip()
                or req.get("source_hash") != hashlib.sha256(req["source"].encode()).hexdigest()):
            raise ValueError("invalid source requirement")
        required.add(req["id"])
    checks = plan.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("executable or explicitly manual checks are required")
    owned = [p for task in tasks for p in task.payload.get("owned_files", ())]
    references = {p for task in tasks for p in task.payload.get("reference_files", {})}
    for path in list(owned) + list(references):
        if (not isinstance(path, str) or not path or "\\" in path or ":" in path
                or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                or str(PurePosixPath(path)) != path):
            raise ValueError("artifact paths must be canonical relative paths")
    if len(owned) != len(set(owned)):
        raise ValueError("artifact ownership overlaps")
    if references & set(owned):
        raise ValueError("reference material cannot be owned by a worker")
    seen, covered = set(), set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"] or check["id"] in seen:
            raise ValueError("invalid or duplicate check ID")
        seen.add(check["id"])
        refs = check.get("requirements")
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in required for r in refs):
            raise ValueError("check must reference known source requirements")
        covered.update(refs)
        kind = check.get("kind")
        if not isinstance(kind, str) or kind not in {"file_exists", "text_contains", "json_equals", "json_match", "manual", "external"}:
            raise ValueError("unsupported check kind")
        if kind == "external":
            names = {name for task in tasks for name in task.payload.get("external_checker_names", [])}
            paths = check.get("paths")
            if (not isinstance(check.get("name"), str) or check["name"] not in names
                    or not isinstance(paths, list) or not paths
                    or any(not isinstance(p, str) or p not in owned for p in paths)):
                raise ValueError("external check must name a trusted checker and owned repair scope")
            continue
        if kind == "manual":
            if not isinstance(check.get("note"), str) or not check["note"].strip():
                raise ValueError("manual checks require review instructions")
            continue
        for key in (("path", "other_path") if kind == "json_match" else ("path",)):
            path = check.get(key)
            if (not isinstance(path, str) or path not in set(owned) | references or "\\" in path
                    or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                    or ":" in path):
                raise ValueError(f"check {check['id']}: {key}={path!r} must be an owned or registered read-only relative artifact")
        if kind == "text_contains" and (not isinstance(check.get("value"), str) or not check["value"]):
            raise ValueError("text checks require a nonempty expected value")
        if kind == "json_equals" and "value" not in check:
            raise ValueError("json_equals requires an expected value")
        for key in (("key", "other_key") if kind == "json_match" else (("key",) if kind == "json_equals" else ())):
            if not isinstance(check.get(key), list) or any(not isinstance(k, str) for k in check[key]):
                raise ValueError("JSON keys must be lists of object keys")
    if covered != required:
        raise ValueError(f"uncovered source requirements: {sorted(required - covered)}")
    proposals = plan.get("decompositions", [])
    if not isinstance(proposals, list):
        raise ValueError("decompositions must be a list")
    all_children = set()
    for proposal in proposals:
        if not isinstance(proposal, dict) or proposal.get("parent") not in {t.id for t in tasks}:
            raise ValueError("invalid decomposition parent")
        children = proposal.get("children")
        if not isinstance(children, list) or len(children) < 2:
            raise ValueError("decomposition requires at least two children")
        files, ids = [], set()
        for child in children:
            if (not isinstance(child, dict) or not isinstance(child.get("id"), str) or not child["id"]
                    or child["id"] in ids or child["id"] in all_children or child["id"] in {t.id for t in tasks}
                    or not isinstance(child.get("postcondition"), str) or not child["postcondition"].strip()
                    or not isinstance(child.get("owned_files"), list) or not child["owned_files"]
                    or any(not isinstance(p, str) for p in child["owned_files"])):
                raise ValueError("invalid decomposition child")
            ids.add(child["id"])
            all_children.add(child["id"])
            files.extend(child["owned_files"])
        parent = next(t for t in tasks if t.id == proposal["parent"])
        if len(files) != len(set(files)) or set(files) != set(parent.payload.get("owned_files", ())):
            raise ValueError("decomposition must preserve artifact ownership exactly")


def evaluate_checks(plan: dict[str, Any], workspace: Path, *, ids: set[str] | None = None,
                    external_checkers=None) -> list[dict[str, Any]]:
    results = []
    for check in plan["checks"]:
        if ids is not None and check["id"] not in ids:
            continue
        result = {"id": check["id"], "requirements": check["requirements"], "status": "unverified", "detail": ""}
        if check["kind"] == "external":
            checker = (external_checkers or {}).get(check["name"])
            if checker is None:
                result["detail"] = "trusted checker is not configured"
            else:
                try:
                    checked = checker(workspace)
                    if checked.get("status") not in {"passed", "failed", "unverified"}:
                        raise ValueError("invalid external checker result")
                    result.update(status=checked["status"], detail=checked.get("detail", ""))
                except Exception as exc:
                    result["detail"] = f"external checker error: {type(exc).__name__}: {exc}"
        elif check["kind"] == "manual":
            result["detail"] = check["note"]
        else:
            try:
                def target(key: str) -> Path:
                    path = (workspace / check[key]).resolve()
                    path.relative_to(workspace.resolve())
                    return path

                def value(path: Path, keys: list[str]) -> Any:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    for key in keys:
                        item = item[key]
                    return item

                path = target("path")
                kind = check["kind"]
                if kind == "file_exists":
                    passed = path.is_file()
                elif kind == "text_contains":
                    passed = check["value"] in path.read_text(encoding="utf-8")
                elif kind == "json_equals":
                    passed = json.dumps(value(path, check["key"]), sort_keys=True) == json.dumps(check["value"], sort_keys=True)
                else:
                    passed = json.dumps(value(path, check["key"]), sort_keys=True) == json.dumps(value(target("other_path"), check["other_key"]), sort_keys=True)
                result["status"] = "passed" if passed else "failed"
                result["detail"] = "predicate satisfied" if passed else "predicate not satisfied"
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                result.update(status="failed", detail=str(exc))
        results.append(result)
    return results
