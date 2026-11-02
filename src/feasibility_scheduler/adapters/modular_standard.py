"""Engineering constraints on planner candidates; no scheduler scoring formula."""
from __future__ import annotations

from .contract_checks import check_paths

STANDARD = "modular-v1"


def ownership_catalog(tasks, plan):
    owned = {t.id: list(t.payload.get("owned_files", ())) for t in tasks}
    owned.update({c["id"]: list(c["owned_files"])
                  for p in plan.get("decompositions", ()) for c in p["children"]})
    return owned


def validate_modular_standard(plan, tasks):
    standard = plan.get("planning_standard")
    if standard is None:
        return  # Supplied older contracts retain compatibility.
    if standard != STANDARD:
        raise ValueError("unknown planning_standard")
    from .acceptance_profile import deferred_checks
    deferred = deferred_checks(plan)
    owned = ownership_catalog(tasks, plan)
    references = {p for t in tasks for p in t.payload.get("reference_files", {})}
    modules = {m["id"]: m for m in plan["modules"]}
    checks = {c["id"]: c for c in plan["checks"]}
    if set(modules) != set(owned):
        raise ValueError("modular-v1 requires a full contract for every active and proposed functional module")
    for mid, module in modules.items():
        if (not isinstance(module.get("boundary"), str) or not module["boundary"].strip()
                or not isinstance(module.get("invariants"), list)
                or any(not isinstance(v, str) or not v.strip() for v in module["invariants"])):
            raise ValueError(f"module {mid}: boundary and explicit invariants are required")
        if (not isinstance(module.get("owned_files"), list)
                or any(not isinstance(p, str) for p in module["owned_files"])
                or len(module["owned_files"]) != len(owned[mid])
                or set(module["owned_files"]) != set(owned[mid])):
            raise ValueError(f"module {mid}: owned_files must match its host artifact boundary")
        budget = module.get("estimated_tokens")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            raise ValueError(f"module {mid}: estimated_tokens must be an explicit positive output-cost estimate")
        if "cohesion_group" in module and (not isinstance(module["cohesion_group"], str) or not module["cohesion_group"].strip()):
            raise ValueError(f"module {mid}: cohesion_group must be nonempty when supplied")
        ids = module.get("local_checks")
        if (not isinstance(ids, list) or not ids or any(not isinstance(cid, str) or cid not in checks for cid in ids)
                or len(ids) != len(set(ids))):
            raise ValueError(f"module {mid}: local_checks must name existing independent checks")
        for cid in ids:
            check = checks[cid]
            paths = check_paths(check)
            # Workspace suites remain root gates. Never pretend they are local evidence.
            if (cid in deferred or check["kind"] in {"manual", "external"}
                    or not paths.intersection(owned[mid]) or not paths <= set(owned[mid]) | references):
                raise ValueError(f"module {mid}: check {cid} is not executable within its local boundary")
        if all(checks[cid]["kind"] in {"file_exists", "text_contains"} for cid in ids):
            if not isinstance(module.get("acceptance_limitations"), str) or not module["acceptance_limitations"].strip():
                raise ValueError(f"module {mid}: structural-only checks require explicit acceptance_limitations")
    for proposal in plan.get("decompositions", ()):
        child_ids = {c["id"] for c in proposal["children"]}
        # Declaring dependencies on a child absent from this replacement would hang it.
        for cid in child_ids:
            dependencies = set(modules[cid]["execution_dependencies"])
            inactive = dependencies - child_ids - {t.id for t in tasks}
            if inactive:
                raise ValueError(f"module {cid}: depends on children of another inactive decomposition")


def functional_mode(task):
    return task.payload.get("contract_plan", {}).get("planning_standard") == STANDARD


def merge_allowed(left, right):
    if not (functional_mode(left) or functional_mode(right)):
        return True
    # A planner must explicitly place both responsibilities in one cohesion group.
    declarations = list(left.payload.get("execution_contract", {}).get("module_contracts", ()))
    declarations += list(right.payload.get("execution_contract", {}).get("module_contracts", ()))
    if not left.payload.get("execution_contract", {}).get("module_contracts") or not right.payload.get("execution_contract", {}).get("module_contracts"):
        return False
    groups = [m.get("cohesion_group") for m in declarations]
    return bool(groups) and all(isinstance(g, str) and g.strip() for g in groups) and len(set(groups)) == 1
