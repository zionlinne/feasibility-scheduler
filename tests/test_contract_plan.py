import json

import pytest

from feasibility_scheduler.adapters.contract_plan import attach_plan, generate_plan
from feasibility_scheduler.adapters.graph_rewrite import generate_candidates
from feasibility_scheduler.adapters.legacy_doc import LegacyDocConfig, run_legacy_doc
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.models import Task


def fixture():
    tasks = [Task(id="a", payload={"owned_files": ["a.py", "b.py"]}),
             Task(id="b", payload={"owned_files": ["c.py"]})]
    plan = {"version": 1, "unresolved": [],
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
    for candidate in generate_candidates(attached, states={}):
        covered = set()
        for task in candidate.tasks:
            assert task.payload["contract_hash"] == attached[0].payload["contract_hash"]
            assert task.payload["contract_plan"] == plan
            assert "SHARED CONTRACT" in task.payload["prompt_text"]
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

