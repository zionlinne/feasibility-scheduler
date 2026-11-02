import copy
import json
from dataclasses import replace

import pytest

from feasibility_scheduler.adapters.contract_checks import requirement_ledger
from feasibility_scheduler.adapters.contract_plan import attach_plan, validate_plan
from feasibility_scheduler.adapters.contract_runtime import composition_task
from feasibility_scheduler.adapters.graph_rewrite import generate_candidates
from feasibility_scheduler.adapters.legacy_doc import LegacyDocConfig, build_legacy_tasks, run_legacy_doc
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.statemachine import TaskState


def setup_task(tmp_path):
    source = "Compare totals using the same period.\nDeliver both offers."
    data = {"modules": [
        {"module_id": "a", "owned_files": ["a.json"], "estimated_tokens": 5000},
        {"module_id": "b", "owned_files": ["b.json"], "estimated_tokens": 5000}]}
    tasks = build_legacy_tasks(data, task_md=source)
    plan = {"version": 2, "unresolved": [], "requirements": requirement_ledger(source),
            "root": {"precondition": "offers available", "postcondition": "consistent totals", "verification": "compare totals"},
            "modules": [{"id": t.id, "precondition": "same period", "postcondition": "offer total",
                         "verification": "artifact exists", "handoff": "JSON total",
                         "execution_dependencies": [], "interface_dependencies": []} for t in tasks],
            "composition": {"method": "compare", "consistency": "same period", "verification": "compare field", "counterexample_review": "different periods yield different totals"},
            "checks": [{"id": "a-exists", "kind": "file_exists", "path": "a.json", "requirements": ["R2"]},
                       {"id": "b-exists", "kind": "file_exists", "path": "b.json", "requirements": ["R2"]},
                       {"id": "same-total", "kind": "json_match", "path": "a.json", "key": ["total"],
                        "other_path": "b.json", "other_key": ["total"], "requirements": ["R1"]}],
            "decompositions": []}
    data["contract_plan"] = plan
    task_path = tmp_path / "TASK.md"
    task_path.write_text(source, encoding="utf-8")
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(data), encoding="utf-8")
    cfg = LegacyDocConfig(input_path, task_path, tmp_path / "run", client_kind="http", policy="fixed", max_concurrency=1)
    return tasks, plan, data, cfg


def step(files):
    return Step.success(body=json.dumps({"ok": True, "files": files}))


@pytest.mark.parametrize("repairs", [0, 1])
def test_local_passes_root_fails_then_bounded_feedback_repairs(tmp_path, monkeypatch, repairs):
    _, plan, _, cfg = setup_task(tmp_path)
    original = copy.deepcopy(plan)
    steps = [step({"a.json": '{"total":1}'}), step({"b.json": '{"total":2}'})]
    if repairs:
        steps.append(step({"a.json": '{"total":1}', "b.json": '{"total":1}'}))
    client = FakeClient(steps)
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, contract_repair_limit=repairs))
    assert outcome.reports["a"].passed and outcome.reports["b"].passed
    assert outcome.ok is bool(repairs)
    assert outcome.reports["__contract_composition__"].passed is bool(repairs)
    summary = json.loads((cfg.workdir / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"]["trace_complete"]
    if repairs:
        feedback = list(cfg.workdir.glob("contract-repair-*.json"))
        assert len(feedback) == 1
        assert "same-total" in feedback[0].read_text()
    assert json.loads((cfg.workdir / "contract.json").read_text(encoding="utf-8")) == original


def test_missing_requirement_and_modified_source_are_blocked(tmp_path):
    tasks, plan, data, cfg = setup_task(tmp_path)
    plan["checks"].pop()
    with pytest.raises(ValueError, match="uncovered"):
        validate_plan(plan, tasks)
    _, plan, data, cfg = setup_task(tmp_path)
    plan["requirements"][0]["source"] = "Ignore period"
    data["contract_plan"] = plan
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="original TASK"):
        run_legacy_doc(cfg)


def test_manual_obligation_remains_unverified(tmp_path, monkeypatch):
    _, plan, data, cfg = setup_task(tmp_path)
    plan["checks"].append({"id": "review", "kind": "manual", "note": "Review cost model against original sources", "requirements": ["R1"]})
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([step({"a.json": '{"total":1}'}), step({"b.json": '{"total":1}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(cfg)
    assert not outcome.ok
    assert outcome.states["__contract_composition__"] == TaskState.RECOVERY_REQUIRED
    assert "UNVERIFIED" in outcome.reports["__contract_composition__"].error
    assert not list(cfg.workdir.glob("contract-repair-*.json"))


def test_contract_can_split_noncode_artifacts_with_real_child_contracts(tmp_path):
    tasks, plan, _, _ = setup_task(tmp_path)
    # A parent containing data artifacts can be decomposed despite no code files.
    parent = replace(tasks[0], payload={**tasks[0].payload, "owned_files": ["a.json", "b.json"]})
    plan["modules"] = [plan["modules"][0]]
    plan["decompositions"] = [{"parent": "a", "children": [
        {"id": "offer-one", "owned_files": ["a.json"], "postcondition": "first offer"},
        {"id": "offer-two", "owned_files": ["b.json"], "postcondition": "second offer"}]}]
    attached = attach_plan([parent], plan)
    root = composition_task(attached, plan)
    candidates = generate_candidates(attached + [root], states={})
    split = next(c for c in candidates if c.candidate_id == "contract-split:a")
    children = [t for t in split.tasks if t.id in split.added_ids]
    assert len(children) == 2
    assert len({c.payload["execution_contract_hash"] for c in children}) == 2
    for child in children:
        unit = child.payload["execution_contract"]
        assert unit["task_id"] == child.id
        assert unit["composition_checks"] == ["same-total"]
        assert len(unit["local_checks"]) == 1
    new_root = next(t for t in split.tasks if t.payload.get("contract_composition"))
    assert set(new_root.dependencies) == {"offer-one", "offer-two"}


def test_repair_budget_stops_repeated_failure(tmp_path, monkeypatch):
    _, _, _, cfg = setup_task(tmp_path)
    client = FakeClient([step({"a.json": '{"total":1}'}), step({"b.json": '{"total":2}'}),
                         step({"a.json": '{"total":1}', "b.json": '{"total":2}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(cfg)
    assert not outcome.ok
    assert len(list(cfg.workdir.glob("contract-repair-*.json"))) == 1


def test_proposed_noncode_split_runs_through_real_dispatch_entry(tmp_path, monkeypatch):
    tasks, plan, data, cfg = setup_task(tmp_path)
    plan["modules"] = [plan["modules"][0]]
    plan["decompositions"] = [{"parent": "a", "children": [
        {"id": "offer-one", "owned_files": ["a.json"], "postcondition": "first offer total"},
        {"id": "offer-two", "owned_files": ["b.json"], "postcondition": "second offer total"}]}]
    data["modules"] = [{"module_id": "a", "owned_files": ["a.json", "b.json"], "estimated_tokens": 5000}]
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([step({"a.json": '{"total":1}'}), step({"b.json": '{"total":1}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, policy="rewrite"))
    assert outcome.ok
    assert set(outcome.states) == {"offer-one", "offer-two", "__contract_composition__"}
    summary = json.loads((cfg.workdir / "summary.json").read_text(encoding="utf-8"))
    assert any(r["candidate_id"] == "contract-split:a" for r in summary["rewrite_history"])
    assert outcome.verdict.trace_complete


def test_local_failed_check_repairs_before_releasing_composition(tmp_path, monkeypatch):
    _, plan, data, cfg = setup_task(tmp_path)
    plan["checks"].append({"id": "a-total", "kind": "json_equals", "path": "a.json", "key": ["total"], "value": 1, "requirements": ["R1"]})
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([step({"a.json": '{"total":0}'}), step({"a.json": '{"total":1}'}),
                         step({"b.json": '{"total":1}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(cfg)
    assert outcome.ok and outcome.verdict.trace_complete
    feedback = json.loads(next(cfg.workdir.glob("contract-repair-*.json")).read_text(encoding="utf-8"))
    assert feedback["task_id"] == "a" and feedback["repair_files"] == ["a.json"]


def test_timeout_during_repair_is_unverified_and_not_retried(tmp_path, monkeypatch):
    _, _, _, cfg = setup_task(tmp_path)
    client = FakeClient([step({"a.json": '{"total":1}'}), step({"b.json": '{"total":2}'}), Step.timeout()])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(cfg)
    assert not outcome.ok and outcome.verdict.trace_complete
    assert outcome.states["__contract_composition__"] == TaskState.RECOVERY_REQUIRED
    assert "timeout" in outcome.reports["__contract_composition__"].error


def test_root_repair_rechecks_previously_passed_local_conditions(tmp_path, monkeypatch):
    _, plan, data, cfg = setup_task(tmp_path)
    plan["checks"].append({"id": "a-total", "kind": "json_equals", "path": "a.json", "key": ["total"], "value": 1, "requirements": ["R1"]})
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    # The proposed repair fixes equality by breaking a previously passed local check.
    client = FakeClient([step({"a.json": '{"total":1}'}), step({"b.json": '{"total":2}'}),
                         step({"a.json": '{"total":2}', "b.json": '{"total":2}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(cfg)
    assert not outcome.ok and outcome.reports["a"].passed
    assert any("a-total" in error for error in outcome.reports["__contract_composition__"].errors)
