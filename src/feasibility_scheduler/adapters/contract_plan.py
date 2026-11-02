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
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise ValueError("contract plan requires version 1")
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


def generate_plan(client: Any, tasks: Sequence[Task], task_md: str) -> dict[str, Any]:
    """Use the configured model once, failing closed on invalid/failed responses."""
    example = {
        "version": 1,
        "unresolved": [],
        "root": {"precondition": "available inputs", "postcondition": "original success criteria", "verification": "how to verify"},
        "modules": [{"id": t.id, "precondition": "inputs needed to start", "postcondition": "observable deliverable",
                     "handoff": "representation, shared terms and behavior", "verification": "independent acceptance procedure",
                     "execution_dependencies": list(t.dependencies), "interface_dependencies": []} for t in tasks],
        "composition": {"method": "how outputs satisfy root", "consistency": "cross-task obligations",
                        "verification": "who checks composition and how", "counterexample_review": "local passes/global failure cases and mitigations"},
    }
    prompt = (
        "Generate a domain-neutral contract plan as one JSON object with the following shape. "
        "Replace descriptive placeholders with concrete requirements grounded in TASK. "
        "Keep module IDs and file ownership. Distinguish source requirements from design decisions in handoff. "
        "Only execution_dependencies require actual upstream artifacts before starting. "
        "Interface dependencies can run in parallel using shared agreed representations. "
        "Preserve all root requirements, define composition checks and review cases where all local "
        "checks pass but the root fails. Put blocking questions in unresolved, otherwise use []. Never invent requirements.\n"
        + canonical(example) + "\nTASK\n" + task_md + "\nMODULES\n"
        + canonical([{"id": t.id, "owned_files": list(t.payload.get("owned_files", ())),
                      "title": t.payload.get("title"), "outputs": list(t.payload.get("outputs", ()))} for t in tasks])
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
        attached.append(replace(task, dependencies=deps, payload=payload))
    return attached
