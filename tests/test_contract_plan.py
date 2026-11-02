import json
from dataclasses import replace

import pytest

from feasibility_scheduler.adapters.contract_plan import attach_plan, generate_plan
from feasibility_scheduler.adapters.graph_rewrite import generate_candidates
from feasibility_scheduler.adapters.legacy_doc import LegacyDocConfig, run_legacy_doc
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.models import Task
from feasibility_scheduler.adapters.contract_checks import requirement_ledger


def fixture():
    tasks = [Task(id="a", payload={"owned_files": ["a.py", "b.py"]}, output_contract={"type": "object"}, constraints_declared=True),
             Task(id="b", payload={"owned_files": ["c.py"]}, output_contract={"type": "object"}, constraints_declared=True)]
    plan = {"version": 2, "unresolved": [], "requirements": requirement_ledger("Compare offers"),
            "checks": [{"id": path, "kind": "file_exists", "path": path, "requirements": ["R1"]}
                       for path in ("a.py", "b.py", "c.py")], "decompositions": [],
            "root": {"precondition": "three offers", "postcondition": "lowest eligible cost", "verification": "recalculate"},
            "modules": [{"id": t.id, "precondition": "shared cost period", "postcondition": "normalized cost",
                         "verification": "check source", "handoff": "three year total",
                         "execution_dependencies": [], "interface_dependencies": []} for t in tasks],
            "composition": {"method": "compare eligible offers", "consistency": "same currency",
                            "verification": "reviewer recalculates minimum", "counterexample_review": "monthly versus annual; normalize"}}
    return tasks, plan


def test_interface_dependency_preserves_parallelism_and_execution_waits():
    tasks, plan = fixture()
    plan["modules"][1]["interface_dependencies"] = ["a"]
    assert attach_plan(tasks, plan)[1].dependencies == ()
    plan["modules"][1]["execution_dependencies"] = ["a"]
    assert attach_plan(tasks, plan)[1].dependencies == ("a",)


@pytest.mark.parametrize("fault", ["cycle", "missing", "unresolved", "verification"])
def test_invalid_plan_blocks_dispatch(fault):
    tasks, plan = fixture()
    if fault == "cycle":
        plan["modules"][0]["execution_dependencies"] = ["b"]
        plan["modules"][1]["execution_dependencies"] = ["a"]
    elif fault == "missing":
        plan["modules"].pop()
    elif fault == "unresolved":
        plan["unresolved"] = ["currency?"]
    else:
        plan["composition"]["verification"] = ""
    with pytest.raises(ValueError):
        attach_plan(tasks, plan)


def test_rewrite_preserves_shared_contract_and_module_coverage():
    tasks, plan = fixture()
    attached = attach_plan(tasks, plan)
    candidates = generate_candidates(attached, states={})
    assert {c.kind for c in candidates} == {"keep", "split", "merge"}
    for candidate in candidates:
        covered = set()
        for task in candidate.tasks:
            assert task.payload["contract_hash"] == attached[0].payload["contract_hash"]
            assert task.payload["contract_plan"] == plan
            assert "SHARED CONTRACT" in task.payload["prompt_text"]
            assert task.payload["execution_contract"]["task_id"] == task.id
            assert task.payload["execution_contract"]["owned_files"] == sorted(task.payload["owned_files"])
            covered.update(task.payload["contract_module_ids"])
        assert covered == {"a", "b"}


def test_model_planner_and_real_adapter_entry_offline(tmp_path):
    tasks, plan = fixture()
    client = FakeClient([Step.success(body=json.dumps(plan))])
    assert generate_plan(client, tasks, "Compare offers") == plan
    input_path = tmp_path / "input.json"
    task_path = tmp_path / "TASK.md"
    task_path.write_text("Compare offers", encoding="utf-8")
    input_path.write_text(json.dumps({"modules": [
        {"module_id": t.id, "owned_files": list(t.payload["owned_files"])} for t in tasks
    ], "contract_plan": plan}), encoding="utf-8")
    outcome = run_legacy_doc(LegacyDocConfig(input_path, task_path, tmp_path / "run", policy="fixed"))
    assert outcome.ok
    assert json.loads((tmp_path / "run" / "contract.json").read_text(encoding="utf-8")) == plan


def hierarchical_fixture():
    tasks, plan = fixture()
    root = replace(tasks[0], id="root", payload={"owned_files": ["a.py", "b.py", "c.py"]})
    declaration = {**plan["modules"][0], "id": "root"}
    children = [{"id": "first", "owned_files": ["a.py", "b.py"], "postcondition": "first deliverable"},
                {"id": "second", "owned_files": ["c.py"], "postcondition": "second deliverable"}]
    plan["decompositions"] = [{"parent": "root", "children": children}]
    plan["modules"] = [declaration] + [
        {**declaration, "id": child["id"], "owned_files": child["owned_files"],
         "execution_dependencies": ["first"] if child["id"] == "second" else []}
        for child in children]
    return [root], plan


def test_child_contract_catalog_is_separate_from_active_graph():
    tasks, plan = hierarchical_fixture()
    attached = attach_plan(tasks, plan)
    assert [t.id for t in attached] == ["root"]
    candidate = next(c for c in generate_candidates(attached, states={}) if c.candidate_id == "contract-split:root")
    by_id = {t.id: t for t in candidate.tasks}
    assert by_id["second"].dependencies == ("first",)
    assert by_id["second"].payload["contract_module_ids"] == ["second"]
    assert by_id["second"].payload["execution_contract"]["module_contracts"][0]["id"] == "second"


@pytest.mark.parametrize("fault", ["undeclared", "ownership", "cycle"])
def test_invalid_child_contracts_still_rejected(fault):
    from feasibility_scheduler.adapters.contract_plan import validate_plan
    tasks, plan = hierarchical_fixture()
    if fault == "undeclared":
        plan["modules"].append({**plan["modules"][0], "id": "not-proposed"})
    elif fault == "ownership":
        plan["modules"][1]["owned_files"] = ["c.py"]
    else:
        plan["modules"][1]["execution_dependencies"] = ["second"]
    with pytest.raises(ValueError):
        validate_plan(plan, tasks)


def test_shared_interface_reference_is_not_an_execution_edge():
    tasks, plan = hierarchical_fixture()
    plan["shared_interfaces"] = {"handler": "handle(state,command,context)"}
    plan["modules"][1]["interface_dependencies"] = ["shared_interfaces"]
    assert attach_plan(tasks, plan)[0].dependencies == ()
    plan["modules"][1]["execution_dependencies"] = ["shared_interfaces"]
    with pytest.raises(ValueError, match="execution_dependencies"):
        attach_plan(tasks, plan)


def test_rejected_plan_has_actionable_persisted_diagnostics(tmp_path):
    tasks, plan = fixture()
    plan["checks"][0]["path"] = "not-owned.py"
    client = FakeClient([Step.success(body=json.dumps(plan))])
    with pytest.raises(ValueError, match="not-owned.py"):
        generate_plan(client, tasks, "Compare offers", audit_dir=tmp_path)
    assert json.loads((tmp_path / "contract-validation.json").read_text())["status"] == "failed"
    assert json.loads((tmp_path / "contract-candidate.json").read_text())["checks"][0]["path"] == "not-owned.py"

