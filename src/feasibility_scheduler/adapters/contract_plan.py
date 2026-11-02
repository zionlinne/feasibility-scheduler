"""Domain-neutral pre-dispatch contracts. Structural checks are not semantic proofs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
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
    if not isinstance(modules, list) or len(modules) != len(ids):
        raise ValueError("contract plan must cover every selected task exactly once")
    seen = set()
    for module in modules:
        require_text(module, ("id", "precondition", "postcondition", "verification", "handoff"))
        mid = module["id"]
        if mid not in ids or mid in seen:
            raise ValueError(f"unknown or duplicate contract module: {mid}")
        seen.add(mid)
        for key in ("execution_dependencies", "interface_dependencies"):
            deps = module.get(key)
            if (not isinstance(deps, list) or any(not isinstance(d, str) or d not in ids or d == mid for d in deps)
                    or len(set(deps)) != len(deps)):
                raise ValueError(f"invalid {key} for {mid}")
    # Existing required waits remain required. Interface references never add waits.
    by_id = {m["id"]: m for m in modules}
    build_dag([
        replace(t, dependencies=tuple(sorted(set(t.dependencies) | set(by_id[t.id]["execution_dependencies"]))))
        for t in tasks
    ])
    if plan["version"] == 2:
        from .contract_checks import validate_checks
        validate_checks(plan, tasks)


def generate_plan(client: Any, tasks: Sequence[Task], task_md: str) -> dict[str, Any]:
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
    example["requirements"] = requirement_ledger(task_md)
    example["checks"] = []
    example["decompositions"] = []
    prompt = (
        "Generate a domain-neutral contract plan as one JSON object with the following shape. "
        "Replace descriptive placeholders with concrete requirements grounded in TASK. "
        "Keep initial module IDs. You may propose decompositions as [{parent: module_id, children: "
        "[{id: unique_id, owned_files: [artifact_path], postcondition: concrete_deliverable}]}], "
        "partitioning the parent's artifacts exactly, including non-code artifacts. "
        "Copy requirements unchanged. Every requirement must reference at least one check via checks[].requirements. "
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
        + canonical(example) + "\nTASK\n" + task_md + "\nMODULES\n"
        + canonical([{"id": t.id, "owned_files": list(t.payload.get("owned_files", ())),
                      "title": t.payload.get("title"), "outputs": list(t.payload.get("outputs", ()))} for t in tasks])
        + "\nREAD-ONLY PROVIDED MATERIALS (may be checked; never assign ownership or modify)\n"
        + canonical(dict(tasks[0].payload.get("reference_files", {})) if tasks else {})
        + "\nTRUSTED EXTERNAL CHECKER NAMES\n"
        + canonical(list(tasks[0].payload.get("external_checker_names", [])) if tasks else [])
        + '\nWhen applicable, use {"id":"integration","kind":"external","name":"trusted-name",'
          '"paths":["owned/output/path"],"requirements":["R1"]} to execute the registered checker. '
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
    plan = json.loads(response.raw_body)
    validate_plan(plan, tasks)
    if plan.get("requirements") != example["requirements"]:
        raise ValueError("planner changed the immutable source requirement ledger")
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
    payload = dict(task.payload)
    owned = set(payload.get("owned_files", ()))
    local, deferred = [], []
    for check in plan["checks"]:
        paths = check_paths(check)
        if paths and paths <= owned:
            local.append(check["id"])
        elif paths & owned:
            deferred.append(check["id"])
    unit = {"task_id": task.id, "owned_files": sorted(owned), "dependencies": list(task.dependencies),
            "local_checks": local, "composition_checks": deferred,
            "deliverable": payload.get("title"),
            "requirements": sorted({r for check in plan["checks"] if check["id"] in local + deferred
                                    for r in check["requirements"]}),
            "root_contract_hash": payload["contract_hash"]}
    payload["execution_contract"] = unit
    payload["execution_contract_hash"] = "sha256:" + hashlib.sha256(canonical(unit).encode()).hexdigest()
    # Replace the previous projection, retaining the immutable shared contract.
    base = str(payload.get("prompt_text", "")).split("\nCURRENT EXECUTION CONTRACT\n", 1)[0]
    payload["prompt_text"] = base + "\nCURRENT EXECUTION CONTRACT\n" + canonical(unit)
    return replace(task, payload=payload)
