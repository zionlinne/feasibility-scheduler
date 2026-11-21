"""Host-owned acceptance obligations, executable bindings and planner responsibilities."""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from feasibility_scheduler.models import Task

ROOT = "__contract_composition__"
PREDICATES = {"file_exists", "text_contains", "json_equals", "json_match", "external"}


def software_profile() -> dict[str, Any]:
    """A small default for CLI software tasks; the host must supply a real suite."""
    descriptions = {
        "requirements": "Original requirements have acceptance evidence",
        "artifacts": "Required artifacts are delivered",
        "build": "The delivered software builds or starts in the required environment",
        "components_interfaces": "Components and their interfaces behave as required",
        "integration": "The composed application works through its entry points",
        "functional": "Original business behavior and edge cases are correct",
        "regression": "Changes preserve previously required behavior",
    }
    return {"id": "software-v1", "categories": [
        {"id": cid, "description": description, "applicable": True,
         "allowed_kinds": sorted(PREDICATES) if cid in {"requirements", "artifacts"} else ["external"],
         "checker_names": ["task-acceptance"]}
        for cid, description in descriptions.items()]}


def _strings(value: Any, label: str, *, empty: bool = False) -> list[str]:
    if (not isinstance(value, list) or (not value and not empty)
            or any(not isinstance(v, str) or not v.strip() for v in value)
            or len(value) != len(set(value))):
        raise ValueError(f"acceptance profile: {label} must be a unique string list")
    return value


def validate_profile(profile: Any, checkers: Mapping[str, Any] | None = None) -> None:
    if (not isinstance(profile, dict) or not isinstance(profile.get("id"), str) or not profile["id"].strip()
            or not isinstance(profile.get("categories"), list) or not profile["categories"]):
        raise ValueError("acceptance profile requires id and categories")
    seen = set()
    for category in profile["categories"]:
        if (not isinstance(category, dict) or not isinstance(category.get("id"), str)
                or not category["id"].strip() or category["id"] in seen
                or not isinstance(category.get("description"), str) or not category["description"].strip()
                or not isinstance(category.get("applicable"), bool)):
            raise ValueError("acceptance profile has invalid or duplicate category")
        cid = category["id"]
        seen.add(cid)
        if not category["applicable"]:
            if not isinstance(category.get("reason"), str) or not category["reason"].strip():
                raise ValueError(f"acceptance category {cid}: host N/A decision requires a reason")
            continue
        kinds = _strings(category.get("allowed_kinds"), f"{cid}.allowed_kinds")
        if not set(kinds) <= PREDICATES:
            raise ValueError(f"acceptance category {cid}: unsupported or manual predicate")
        names = _strings(category.get("checker_names", []), f"{cid}.checker_names", empty=True)
        if "external" in kinds and not names:
            raise ValueError(f"acceptance category {cid}: external checker_names are required")
        if checkers is not None and set(kinds) == {"external"} and not any(callable(checkers.get(n)) for n in names):
            raise ValueError(f"acceptance category {cid}: no executable checker registered; expected one of {names}")


def bind_profile(plan: dict[str, Any], profile: dict[str, Any]) -> None:
    """Only the host selects applicability and evidence policy; do not trust model copies."""
    if "acceptance_profile" in plan and plan["acceptance_profile"] != profile:
        raise ValueError("planner changed the host acceptance profile")
    plan["acceptance_profile"] = copy.deepcopy(profile)


def complete_root_references(plan: dict[str, Any], tasks: Sequence[Task]) -> list[dict[str, Any]]:
    """Compile missing root evidence links, without inventing tests or coverage claims.

    Every check already runs at composition. Linking its declared source IDs to
    another compatible root obligation changes only the evidence index. Local
    ownership, requirement sets and predicate definitions are never rewritten.
    """
    profile = plan.get("acceptance_profile")
    if profile is None:
        return []
    validate_profile(profile)
    from .contract_checks import validate_checks
    validate_checks(plan, tasks)
    categories = {c["id"]: c for c in profile["categories"] if c["applicable"]}
    checks = {c["id"]: c for c in plan["checks"]}
    known = {r["id"] for r in plan["requirements"]}
    repairs = []
    assignments = plan.get("acceptance_assignments")
    if not isinstance(assignments, list):
        return repairs  # The strict validator reports malformed/missing assignments.
    for assignment in assignments:
        if (not isinstance(assignment, dict) or assignment.get("scope") != "root"
                or assignment.get("owners") != [ROOT]):
            continue
        cid = assignment.get("category")
        if not isinstance(cid, str) or cid not in categories:
            continue
        refs = _strings(assignment.get("requirements"), "assignment.requirements")
        attached = _strings(assignment.get("checks"), "assignment.checks")
        if not set(refs) <= known or not set(attached) <= checks.keys():
            continue
        covered = {r for check_id in attached for r in checks[check_id]["requirements"]}
        missing = set(refs) - covered
        if not missing:
            continue
        category = categories[cid]
        candidates = [check for check in plan["checks"]
                      if check["id"] not in attached and missing.intersection(check["requirements"])
                      and check["kind"] in category["allowed_kinds"]
                      and (check["kind"] != "external" or check["name"] in category.get("checker_names", []))]
        available = {r for check in candidates for r in check["requirements"]}
        if not missing <= available:
            continue  # Never shrink the obligation or substitute weaker evidence.
        added = [check["id"] for check in candidates]
        assignment["checks"] = attached + added
        repairs.append({"assignment_id": assignment.get("id"), "category": cid,
                        "missing_requirements": sorted(missing), "added_checks": added,
                        "reason": "Link existing category-approved evidence to root obligation"})
    return repairs


def validate_responsibilities(plan: dict[str, Any], tasks: Sequence[Task], *, checkers=None) -> None:
    profile = plan.get("acceptance_profile")
    if profile is None:
        return  # Explicit legacy/library compatibility; the real CLI supplies a profile.
    validate_profile(profile, checkers)
    categories = {c["id"]: c for c in profile["categories"] if c["applicable"]}
    checks = {c["id"]: c for c in plan["checks"]}
    # A dangling manual check also blocks composition, even if omitted from assignments.
    for check in checks.values():
        if check["kind"] == "manual":
            raise ValueError(f"acceptance check {check['id']}: manual has no executable evidence")
        if check["kind"] == "external" and checkers is not None and not callable(checkers.get(check["name"])):
            raise ValueError(f"acceptance check {check['id']}: checker {check['name']} is not callable")
    owned = {t.id: set(t.payload.get("owned_files", ())) for t in tasks}
    for proposal in plan.get("decompositions", []):
        owned.update({c["id"]: set(c["owned_files"]) for c in proposal["children"]})
    references = {p for t in tasks for p in t.payload.get("reference_files", {})}
    requirements = {r["id"] for r in plan["requirements"]}
    assignments = plan.get("acceptance_assignments")
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("acceptance_assignments are required by the host profile")
    covered_categories, covered_requirements, assigned_checks, seen = set(), set(), set(), set()
    from .contract_checks import check_paths
    for assignment in assignments:
        if (not isinstance(assignment, dict) or not isinstance(assignment.get("id"), str)
                or not assignment["id"].strip() or assignment["id"] in seen):
            raise ValueError("invalid or duplicate acceptance assignment ID")
        aid = assignment["id"]
        seen.add(aid)
        cid = assignment.get("category")
        if not isinstance(cid, str) or cid not in categories:
            raise ValueError(f"acceptance assignment {aid}: unknown or N/A category")
        category = categories[cid]
        refs = _strings(assignment.get("requirements"), f"{aid}.requirements")
        owners = _strings(assignment.get("owners"), f"{aid}.owners")
        check_ids = _strings(assignment.get("checks"), f"{aid}.checks")
        if not set(refs) <= requirements or not set(check_ids) <= checks.keys():
            raise ValueError(f"acceptance assignment {aid}: unknown requirement or check")
        scope = assignment.get("scope")
        if (not isinstance(scope, str) or scope not in {"local", "cross", "root"}
                or (scope == "root" and owners != [ROOT])
                or (scope != "root" and not set(owners) <= owned.keys())
                or (scope == "local" and len(owners) != 1)
                or (scope == "cross" and len(owners) < 2)):
            raise ValueError(f"acceptance assignment {aid}: invalid scope or owners")
        evidence_requirements = set()
        for check_id in check_ids:
            check = checks[check_id]
            if check["kind"] not in category["allowed_kinds"]:
                raise ValueError(f"acceptance category {cid}: check {check_id} has insufficient evidence kind {check['kind']}")
            if check["kind"] == "external":
                if check["name"] not in category.get("checker_names", []):
                    raise ValueError(f"acceptance category {cid}: checker {check['name']} is not approved")
                if scope != "root":
                    raise ValueError(f"acceptance assignment {aid}: workspace external checks must run at root")
            if scope != "root" and not check_paths(check) <= references | set().union(*(owned[o] for o in owners)):
                raise ValueError(f"acceptance assignment {aid}: check paths exceed responsible owners")
            evidence_requirements.update(check["requirements"])
        if not set(refs) <= evidence_requirements:
            raise ValueError(f"acceptance assignment {aid}: checks do not cover its requirements; "
                             f"missing {sorted(set(refs) - evidence_requirements)}; attached checks {check_ids}")
        covered_categories.add(cid)
        covered_requirements.update(refs)
        assigned_checks.update(check_ids)
    if covered_categories != categories.keys():
        raise ValueError(f"missing acceptance categories: {sorted(categories.keys() - covered_categories)}")
    if covered_requirements != requirements:
        raise ValueError(f"unassigned source requirements: {sorted(requirements - covered_requirements)}")
    if assigned_checks != checks.keys():
        raise ValueError(f"unassigned acceptance checks: {sorted(checks.keys() - assigned_checks)}")


def deferred_checks(plan: dict[str, Any]) -> set[str]:
    return {cid for a in plan.get("acceptance_assignments", []) if a["scope"] != "local" for cid in a["checks"]}


def project_responsibilities(task: Task, local: list[str], deferred: list[str]) -> list[dict[str, Any]]:
    """Use immutable assignment IDs and check paths through split/merge rewrites."""
    plan = task.payload["contract_plan"]
    relevant = set(local + deferred)
    identities = {task.id, *task.payload.get("contract_module_ids", [])}
    return [copy.deepcopy(a) for a in plan.get("acceptance_assignments", [])
            if relevant.intersection(a["checks"]) or identities.intersection(a["owners"])]
