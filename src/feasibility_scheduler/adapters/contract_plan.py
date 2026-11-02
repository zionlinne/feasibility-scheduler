"""Domain-neutral pre-dispatch contracts. Structural checks are not semantic proofs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from feasibility_scheduler.dag import build_dag
from feasibility_scheduler.fake_client import ApiRequest, Outcome
from feasibility_scheduler.models import Task


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_plan(plan: Any, tasks: Sequence[Task]) -> None:
    """Reject incomplete plans before any work is dispatched."""
    if not isinstance(plan, dict) or plan.get("version") not in (1, 2):
        raise ValueError("contract plan requires version 1 or 2")
    if plan.get("unresolved") != []:
        raise ValueError("contract plan must explicitly resolve all blocking questions")
    def require_text(obj: Any, keys: Sequence[str]) -> None:
        if not isinstance(obj, dict) or any(
            not isinstance(obj.get(key), str) or not obj[key].strip() for key in keys
        ):
            raise ValueError(f"contract plan requires nonempty fields: {keys}")

    require_text(plan.get("root"), ("precondition", "postcondition", "verification"))
    require_text(plan.get("composition"), ("method", "consistency", "verification", "counterexample_review"))
    modules = plan.get("modules")
    ids = {task.id for task in tasks}
    if not isinstance(modules, list):
        raise ValueError("contract plan must cover every selected task exactly once")
    children = {}
    if plan["version"] == 2:
        from .contract_checks import validate_checks
        validate_checks(plan, tasks)
        children = {child["id"]: (proposal["parent"], child)
                    for proposal in plan.get("decompositions", []) for child in proposal["children"]}
    allowed_ids = ids | children.keys()
    interface_ids = set(allowed_ids)
    if isinstance(plan.get("shared_interfaces"), dict) and plan["shared_interfaces"]:
        interface_ids.add("shared_interfaces")
    seen = set()
    for module in modules:
        require_text(module, ("id", "precondition", "postcondition", "verification", "handoff"))
        mid = module["id"]
        if mid not in allowed_ids or mid in seen:
            raise ValueError(f"unknown or duplicate contract module: {mid}")
        seen.add(mid)
        for key in ("execution_dependencies", "interface_dependencies"):
            deps = module.get(key)
            targets = interface_ids if key == "interface_dependencies" else allowed_ids
            if (not isinstance(deps, list) or any(not isinstance(d, str) or d not in targets or d == mid for d in deps)
                    or len(set(deps)) != len(deps)):
                raise ValueError(f"invalid {key} for {mid}")
        if mid in ids and not set(module["execution_dependencies"]) <= ids:
            raise ValueError(f"active module {mid} cannot wait for an inactive proposed child")
        if mid in children:
            parent, proposal = children[mid]
            if parent in module["execution_dependencies"]:
                raise ValueError(f"child {mid} cannot wait for its replaced parent {parent}")
            if "owned_files" in module:
                owned = module["owned_files"]
                if (not isinstance(owned, list) or any(not isinstance(p, str) for p in owned)
                        or len(owned) != len(proposal["owned_files"])
                        or set(owned) != set(proposal["owned_files"])):
                    raise ValueError(f"child contract ownership differs from decomposition: {mid}")
    if not ids <= seen:
        raise ValueError(f"contract plan missing selected tasks: {sorted(ids - seen)}")
    # Existing required waits remain required. Interface references never add waits.
    by_id = {m["id"]: m for m in modules}
    parent_tasks = {t.id: t for t in tasks}
    # Child declarations describe future work, not extra active tasks. Validate
    # their reference graph now; actual ownership/dependency rewrites remain gated.
    build_dag([
        replace(t, dependencies=tuple(sorted(set(t.dependencies) | set(by_id[t.id]["execution_dependencies"]))))
        for t in tasks
    ] + [Task(id=cid, dependencies=tuple(by_id[cid]["execution_dependencies"])
              if cid in by_id else parent_tasks[parent].dependencies)
         for cid, (parent, _) in children.items()])
    from .acceptance_profile import validate_responsibilities
    for task in tasks:
        expected = task.payload.get("acceptance_profile")
        if expected is not None and plan.get("acceptance_profile") != expected:
            raise ValueError("contract differs from the host acceptance profile")
    validate_responsibilities(plan, tasks)


def bind_source_requirements(plan: Any, task_md: str) -> list[str]:
    """Bind model references to host-owned source; model source/hash fields are never authoritative."""
    from .contract_checks import requirement_ledger
    ledger = requirement_ledger(task_md)
    if not isinstance(plan, dict) or not isinstance(plan.get("requirements"), list):
        raise ValueError("planner must return source requirement IDs")
    rows = plan["requirements"]
    ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
    expected = [row["id"] for row in ledger]
    if (any(not isinstance(rid, str) for rid in ids) or len(ids) != len(set(ids))
            or set(ids) != set(expected)):
        raise ValueError("planner source requirement IDs must match the host ledger exactly (no missing, duplicate, or unknown IDs)")
    replaced = [row["id"] for row in rows if row != next(r for r in ledger if r["id"] == row["id"])
                and ("source" in row or "source_hash" in row)]
    plan["requirements"] = ledger
    return replaced


def generate_plan(client: Any, tasks: Sequence[Task], task_md: str, *, audit_dir: Path | None = None) -> dict[str, Any]:
    """Use the configured model once, failing closed on invalid/failed responses."""
    example = {
        "version": 2,
        "unresolved": [],
        "root": {"precondition": "available inputs", "postcondition": "original success criteria", "verification": "how to verify"},
        "modules": [{"id": t.id, "precondition": "inputs needed to start", "postcondition": "observable deliverable",
                     "handoff": "representation, shared terms and behavior", "verification": "independent acceptance procedure",
                     "execution_dependencies": list(t.dependencies), "interface_dependencies": []} for t in tasks],
        "composition": {"method": "how outputs satisfy root", "consistency": "cross-task obligations",
                        "verification": "who checks composition and how", "counterexample_review": "local passes/global failure cases and mitigations"},
    }
    from .contract_checks import requirement_ledger
    ledger = requirement_ledger(task_md)
    example["requirements"] = [{"id": row["id"]} for row in ledger]
    example["checks"] = []
    example["decompositions"] = []
    profile = tasks[0].payload.get("acceptance_profile") if tasks else None
    profile_prompt = ""
    if profile is not None:
        example["acceptance_assignments"] = []
        profile_prompt = (
            "\nHOST ACCEPTANCE PROFILE (immutable; do not copy or change it)\n" + canonical(profile)
            + '\nReturn acceptance_assignments: [{"id":"unique", "category":"profile category ID", '
              '"requirements":["R1"], "owners":["module ID"], "checks":["check ID"], "scope":"local"}]. '
              'Cover every applicable category, every source requirement and every check. '
              'A local assignment has one owner; cross has at least two; root has only '
              '__contract_composition__ as owner. Owners may reference proposed children. '
              'Cross/root checks run after integration. External workspace checkers must use root scope. '
              'Use only category-approved predicate kinds and checker names. manual is not executable '
              'and cannot satisfy this profile. Never substitute file or syntax checks for business tests. '
              'The same independent suite may support several categories only if it really tests them. '
              'If verification capability or coverage is missing, describe it in unresolved; do not invent evidence. '
              'Keep decomposition free: the profile specifies obligations, not module count or a fixed DAG.\n'
        )
    prompt = (
        "Generate a domain-neutral contract plan as one JSON object with the following shape. "
        "Replace descriptive placeholders with concrete requirements grounded in TASK. "
        "Keep initial module IDs. You may propose decompositions as [{parent: module_id, children: "
        "[{id: unique_id, owned_files: [artifact_path], postcondition: concrete_deliverable}]}], "
        "partitioning the parent's artifacts exactly, including non-code artifacts. "
        "modules must contain each initial task contract exactly once. It may ALSO contain detailed "
        "contracts for children explicitly declared in decompositions; these are future contracts, not "
        "additional active tasks. Child execution_dependencies are applied when FBS selects that split. "
        "interface_dependencies may reference module IDs or the provided shared_interfaces contract; "
        "shared_interfaces is a specification reference and must never be an execution dependency. "
        "Return requirements as ID-only objects, e.g. {id: R1}; do not copy source text or hashes. "
        "The host owns and attaches the immutable source ledger below. "
        "Every requirement must reference at least one check via checks[].requirements. "
        'Every check object MUST have "id" (unique string), "kind" (predicate name), and "requirements" '
        '(source ID list). Use the field name "kind", never "type". Example check object: '
        '{"id":"exact-artifact","kind":"json_equals","path":"artifact.json","key":[],"value":{"x":1},"requirements":["R1"]}. '
        "Adapt the example path, value, IDs and predicate to TASK. "
        "Checks support file_exists(path), text_contains(path,value), json_equals(path,key,value), "
        "json_match(path,key,other_path,other_key), and manual(note). key/other_key are lists of object keys. "
        "An empty key [] selects the ENTIRE JSON value. For example, json_equals with key=[] "
        "and value={\"x\":1} checks the exact object, including its key set, and rejects extra keys. "
        "Use whole-object equality when TASK specifies an exact JSON object; no manual key-set review is needed. "
        "Populate checks with the smallest sufficient executable predicates. Do not add a blanket root-review "
        "or a manual duplicate of an executable check. Use manual only for a specific mandatory obligation "
        "that none of the supported predicates can establish; manual remains UNVERIFIED. "
        "Distinguish mandatory outcomes from permissions and explanatory context. A statement such as "
        "'may run in parallel' permits a scheduling choice; it does not require proving parallelism "
        "or human approval. Keep that source line in requirements and reference it from the relevant "
        "deliverable checks, without inventing additional mandatory acceptance conditions. "
        "Distinguish source requirements from design decisions in handoff. "
        "Only execution_dependencies require actual upstream artifacts before starting. "
        "Interface dependencies can run in parallel using shared agreed representations. "
        "Preserve all root requirements, define composition checks and review cases where all local "
        "checks pass but the root fails. Put blocking questions in unresolved, otherwise use []. Never invent requirements.\n"
        + profile_prompt + canonical(example) + "\nHOST SOURCE LEDGER (read-only; reference IDs only)\n" + canonical(ledger)
        + "\nTASK\n" + task_md + "\nMODULES\n"
        + canonical([{"id": t.id, "owned_files": list(t.payload.get("owned_files", ())),
                      "title": t.payload.get("title"), "outputs": list(t.payload.get("outputs", ()))} for t in tasks])
        + "\nREAD-ONLY PROVIDED MATERIALS (may be checked; never assign ownership or modify)\n"
        + canonical(dict(tasks[0].payload.get("reference_files", {})) if tasks else {})
        + "\nTRUSTED EXTERNAL CHECKER NAMES\n"
        + canonical(list(tasks[0].payload.get("external_checker_names", [])) if tasks else [])
        + '\nWhen applicable, use {"id":"integration","kind":"external","name":"trusted-name",'
          '"read_paths":["owned/output/path","provided/input/path"],'
          '"repair_paths":["owned/output/path"],"requirements":["R1"]} to execute the registered checker. '
          'read_paths lists artifacts/materials relevant to verification, including registered read-only inputs. '
          'repair_paths lists only owned outputs that may be replaced after failure; use [] if nothing is repairable. '
          'Never assign ownership or repair permission to provided materials. Do not use the legacy paths field. '
          'Only registered names are valid; no shell commands can be supplied by the model. '
          'A complete supplied black-box suite can discharge requirements it tests; retain manual obligations '
          'for requirements outside its coverage. Include concrete shared function signatures and state shape '
          'in an additional shared_interfaces object and module handoff so workers can implement in parallel.'
    )
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    response = client.call(ApiRequest(
        task_id="contract_planning", idempotency_key=f"contract-plan:{digest}", prompt=prompt,
        model=client.model, provider=client.provider,
    ))
    if response.outcome != Outcome.SUCCESS:
        raise ValueError(f"contract planning failed: {response.outcome}")
    validation = {"selected_task_ids": [t.id for t in tasks],
                  "registered_reference_paths": sorted(tasks[0].payload.get("reference_files", {})) if tasks else [],
                  "status": "failed"}
    try:
        plan = json.loads(response.raw_body)
        if audit_dir is not None:
            audit_dir.mkdir(parents=True, exist_ok=True)
            (audit_dir / "contract-candidate.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        validation["source_ledger_owner"] = "host"
        validation["ignored_model_source_fields"] = bind_source_requirements(plan, task_md)
        if profile is not None:
            from .acceptance_profile import bind_profile, complete_root_references
            bind_profile(plan, profile)
            validation["completed_root_references"] = complete_root_references(plan, tasks)
        validate_plan(plan, tasks)
        validation["status"] = "passed"
    except (ValueError, TypeError) as exc:
        validation["error"] = str(exc)
        raise
    finally:
        if audit_dir is not None:
            audit_dir.mkdir(parents=True, exist_ok=True)
            (audit_dir / "contract-validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def attach_plan(tasks: Sequence[Task], plan: dict[str, Any]) -> list[Task]:
    validate_plan(plan, tasks)
    digest = "sha256:" + hashlib.sha256(canonical(plan).encode()).hexdigest()
    by_id = {m["id"]: m for m in plan["modules"]}
    attached = []
    for task in tasks:
        deps = tuple(sorted(set(task.dependencies) | set(by_id[task.id]["execution_dependencies"])))
        payload = dict(task.payload)
        payload.update(contract_plan=plan, contract_hash=digest, contract_module_ids=[task.id], deps=list(deps))
        payload["prompt_text"] = str(payload.get("prompt_text", "")) + "\nSHARED CONTRACT " + digest + "\n" + canonical(plan)
        attached.append(refresh_contract(replace(task, dependencies=deps, payload=payload)))
    return attached


def refresh_contract(task: Task) -> Task:
    """Project obligations onto the current work unit after a split or merge."""
    plan = task.payload.get("contract_plan")
    if not plan or plan.get("version") != 2 or task.payload.get("contract_composition"):
        return task
    from .contract_checks import check_paths
    from .acceptance_profile import deferred_checks, project_responsibilities
    payload = dict(task.payload)
    owned = set(payload.get("owned_files", ()))
    root_checks = deferred_checks(plan)
    local, deferred = [], []
    for check in plan["checks"]:
        paths = check_paths(check)
        if paths and paths <= owned and check["id"] not in root_checks:
            local.append(check["id"])
        elif paths & owned:
            deferred.append(check["id"])
    unit = {"task_id": task.id, "owned_files": sorted(owned), "dependencies": list(task.dependencies),
            "local_checks": local, "composition_checks": deferred,
            "deliverable": payload.get("title"),
            "requirements": sorted({r for check in plan["checks"] if check["id"] in local + deferred
                                    for r in check["requirements"]}),
            "root_contract_hash": payload["contract_hash"]}
    declarations = {m["id"]: m for m in plan["modules"]}
    if task.id in declarations:
        payload["contract_module_ids"] = [task.id]
    unit["module_contracts"] = [declarations[mid] for mid in payload.get("contract_module_ids", [])
                                if mid in declarations]
    if "acceptance_profile" in plan:
        unit["acceptance_responsibilities"] = project_responsibilities(task, local, deferred)
    payload["execution_contract"] = unit
    payload["execution_contract_hash"] = "sha256:" + hashlib.sha256(canonical(unit).encode()).hexdigest()
    # Replace the previous projection, retaining the immutable shared contract.
    base = str(payload.get("prompt_text", "")).split("\nCURRENT EXECUTION CONTRACT\n", 1)[0]
    payload["prompt_text"] = base + "\nCURRENT EXECUTION CONTRACT\n" + canonical(unit)
    return replace(task, payload=payload)
