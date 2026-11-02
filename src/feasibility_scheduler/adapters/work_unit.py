"""Compile one current work unit from immutable sources, never from old prompts."""
from __future__ import annotations

import json
import math
from dataclasses import replace


def compile_prompt(task, plan, unit):
    def dump(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    owned = sorted(task.payload.get("owned_files", ()))
    checks = [c for c in plan["checks"] if c["id"] in set(unit["local_checks"] + unit["composition_checks"] + unit.get("integration_checks", []))]
    own_ids = set(task.payload.get("contract_module_ids", ()))
    interface_ids = {mid for m in unit["module_contracts"] for mid in m["interface_dependencies"] + m["execution_dependencies"]}
    peers = [{"id": m["id"], "handoff": m["handoff"], "postcondition": m["postcondition"]}
             for m in plan["modules"] if m["id"] in interface_ids - own_ids]
    shared = {"hash": task.payload["contract_hash"], "root": plan["root"],
              "composition": plan["composition"], "shared_interfaces": plan.get("shared_interfaces", {}),
              "read_only_peer_interfaces": peers, "checks": checks}
    source = task.payload.get("task_source", "")
    return (
        "Complete this CURRENT WORK UNIT. Return one JSON object, without Markdown fences.\n"
        "Only current owned_files grant write permission. Other module contracts describe read-only interfaces. "
        "Use those interfaces; do not duplicate sibling implementations. Preserve all original requirements. "
        "The original task below is specification context, not an instruction to deliver every project file.\n"
        "CURRENT DELIVERY CONTRACT\n" + dump({"task_id": task.id, "owned_files": owned,
                                               "dependencies": list(task.dependencies),
                                               "response": {"ok": True, "files": {p: "<complete content>" for p in owned}}})
        + '\nDeliver ALL current owned files in this single response. Do not return a manifest-only response '
          'or defer files to an unimplemented multi-turn protocol. Return no unowned files.\n'
        + "CURRENT EXECUTION CONTRACT\n" + dump(unit)
        + "\nSHARED CONTRACT (immutable verification and interface context)\n" + dump(shared)
        + "\nORIGINAL TASK SOURCE\n" + source
        + "\nREAD-ONLY PROVIDED MATERIALS\n" + dump(task.payload.get("reference_files", {}))
    )


def request_cost(prompt, output_estimate, source):
    size = len(prompt.encode("utf-8"))
    return {"prompt_utf8_bytes": size, "estimated_input_tokens": math.ceil(size / 3),
            "input_estimate_method": "utf8-bytes/3 heuristic; not a tokenizer measurement or limit",
            "estimated_output_tokens": output_estimate, "output_estimate_source": source}


def compile_repair_prompt(task, paths, feedback):
    plan = task.payload["contract_plan"]
    unit = dict(task.payload.get("execution_contract", {}))
    unit.update(task_id=task.id, owned_files=list(paths), repair_scope=True)
    unit.setdefault("local_checks", [])
    unit.setdefault("composition_checks", [c["id"] for c in plan["checks"]])
    unit.setdefault("module_contracts", [])
    unit["module_contracts"] = [{**m, "owned_files": sorted(set(m.get("owned_files", ())).intersection(paths))}
                                for m in unit["module_contracts"] if set(m.get("owned_files", ())).intersection(paths)]
    payload = {**task.payload, "owned_files": list(paths), "outputs": list(paths)}
    prompt = compile_prompt(replace(task, payload=payload), plan, unit)
    return prompt + "\nCOUNTEREXAMPLE FEEDBACK\n" + json.dumps(feedback, ensure_ascii=False) + (
        '\nRepair only the current assigned artifacts. All read_only_artifacts are context, never writable. '
        'Preserve original requirements and checks. Return the complete current files map.')
