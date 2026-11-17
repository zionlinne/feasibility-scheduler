import copy
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from feasibility_scheduler.adapters.acceptance_profile import ROOT, software_profile, validate_profile
from feasibility_scheduler.adapters.contract_plan import attach_plan, generate_plan, validate_plan
from feasibility_scheduler.adapters.graph_rewrite import generate_candidates
from feasibility_scheduler.adapters.legacy_doc import run_legacy_doc
from feasibility_scheduler.fake_client import FakeClient, Step
from test_contract_plan import hierarchical_fixture
from test_contract_runtime import setup_task, step


def profile():
    return {"id": "comparison-v1", "categories": [
        {"id": "artifacts", "description": "Both offers exist", "applicable": True,
         "allowed_kinds": ["file_exists"], "checker_names": []},
        {"id": "functional", "description": "Comparable offers have equal totals", "applicable": True,
         "allowed_kinds": ["json_match"], "checker_names": []},
        {"id": "http", "description": "HTTP interface", "applicable": False, "reason": "No HTTP interface requested"}]}


def assignments():
    return [
        {"id": "a-artifact", "category": "artifacts", "requirements": ["R2"],
         "owners": ["a"], "checks": ["a-exists"], "scope": "local"},
        {"id": "b-artifact", "category": "artifacts", "requirements": ["R2"],
         "owners": ["b"], "checks": ["b-exists"], "scope": "local"},
        {"id": "same-period", "category": "functional", "requirements": ["R1"],
         "owners": ["a", "b"], "checks": ["same-total"], "scope": "cross"}]


def apply_profile(tasks, plan):
    p = profile()
    plan["acceptance_profile"] = p
    plan["acceptance_assignments"] = assignments()
    return [replace(t, payload={**t.payload, "acceptance_profile": copy.deepcopy(p)}) for t in tasks]


def test_default_software_profile_requires_real_checker_not_syntax_mode():
    with pytest.raises(ValueError, match="no executable checker registered"):
        validate_profile(software_profile(), {})
    with pytest.raises(ValueError, match="no executable checker registered"):
        validate_profile(software_profile(), {"task-acceptance": "not callable"})
    validate_profile(software_profile(), {"task-acceptance": lambda _: {"status": "passed"}})


def test_missing_capability_blocks_before_client_creation(tmp_path, monkeypatch):
    _, _, _, cfg = setup_task(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("must fail before creating or calling the model client")
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", forbidden)
    with pytest.raises(ValueError, match="task-acceptance"):
        run_legacy_doc(replace(cfg, acceptance_profile=software_profile()))
    assert not (cfg.workdir / "events.jsonl").exists()
    audit = json.loads((cfg.workdir / "acceptance-preflight.json").read_text())
    assert audit["status"] == "failed" and audit["phase"] == "capabilities"


def test_generated_profile_is_host_bound_and_prompt_has_responsibilities(tmp_path):
    tasks, plan, _, _ = setup_task(tmp_path)
    tasks = apply_profile(tasks, plan)
    del plan["acceptance_profile"]  # The model need not transcribe the host profile.
    client = FakeClient([Step.success(body=json.dumps(plan))])
    result = generate_plan(client, tasks, "Compare totals using the same period.\nDeliver both offers.")
    assert result["acceptance_profile"] == profile()
    assert result["acceptance_assignments"] == assignments()
    assert result["requirements"] == plan["requirements"]


@pytest.mark.parametrize("fault,match", [
    ("missing-category", "missing acceptance categories"),
    ("unknown-owner", "invalid scope or owners"),
    ("paths", "paths exceed responsible owners"),
    ("coverage", "checks do not cover"),
    ("manual", "manual has no executable evidence"),
    ("weak-functional", "insufficient evidence kind"),
    ("host-profile", "host acceptance profile"),
    ("na-category", "unknown or N/A"),
    ("unassigned-check", "unassigned acceptance checks"),
])
def test_invalid_responsibilities_are_rejected(tmp_path, fault, match):
    tasks, plan, _, _ = setup_task(tmp_path)
    tasks = apply_profile(tasks, plan)
    mapping = plan["acceptance_assignments"]
    if fault == "missing-category":
        mapping.pop()
    elif fault == "unknown-owner":
        mapping[0]["owners"] = ["invented"]
    elif fault == "paths":
        mapping[0]["owners"] = ["b"]
    elif fault == "coverage":
        mapping[0]["requirements"] = ["R1"]
    elif fault == "manual":
        plan["checks"].append({"id": "review", "kind": "manual", "note": "Review", "requirements": ["R1"]})
    elif fault == "weak-functional":
        mapping[-1]["checks"] = ["a-exists"]
    elif fault == "host-profile":
        plan["acceptance_profile"] = copy.deepcopy(plan["acceptance_profile"])
        plan["acceptance_profile"]["categories"][1]["applicable"] = False
    elif fault == "na-category":
        mapping[0]["category"] = "http"
    else:
        plan["checks"].append({"id": "extra", "kind": "file_exists", "path": "a.json", "requirements": ["R1"]})
    with pytest.raises(ValueError, match=match):
        validate_plan(plan, tasks)


def test_supplied_plan_missing_assignments_blocks_before_dispatch(tmp_path, monkeypatch):
    _, _, data, cfg = setup_task(tmp_path)
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    with pytest.raises(ValueError, match="acceptance_assignments"):
        run_legacy_doc(replace(cfg, acceptance_profile=profile()))
    assert not (cfg.workdir / "events.jsonl").exists()


def test_responsibility_plan_runs_and_root_enforces_real_counterexample(tmp_path, monkeypatch):
    _, plan, data, cfg = setup_task(tmp_path)
    plan["acceptance_assignments"] = assignments()
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([step({"a.json": '{"total":1}'}), step({"b.json": '{"total":2}'}),
                         step({"a.json": '{"total":1}', "b.json": '{"total":1}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, acceptance_profile=profile()))
    assert outcome.ok and outcome.verdict.trace_complete
    assert len(list(cfg.workdir.glob("contract-repair-*.json"))) == 1
    stored = json.loads((cfg.workdir / "contract.json").read_text())
    assert stored["acceptance_profile"] == profile()
    assert stored["acceptance_assignments"] == assignments()


def test_root_scope_does_not_run_early_on_single_file_and_requires_callable(tmp_path):
    tasks, plan, _, _ = setup_task(tmp_path)
    p = {"id": "business", "categories": [{"id": "functional", "description": "Real suite",
         "applicable": True, "allowed_kinds": ["external"], "checker_names": ["suite"]}]}
    tasks = [replace(t, payload={**t.payload, "external_checker_names": ["suite"], "acceptance_profile": p}) for t in tasks]
    plan.update(acceptance_profile=p, checks=[{"id": "business", "kind": "external", "name": "suite",
                "paths": ["a.json"], "requirements": ["R1", "R2"]}], acceptance_assignments=[
                    {"id": "root-suite", "category": "functional", "requirements": ["R1", "R2"],
                     "owners": [ROOT], "checks": ["business"], "scope": "root"}])
    attached = attach_plan(tasks, plan)
    assert attached[0].payload["execution_contract"]["local_checks"] == []
    assert attached[0].payload["execution_contract"]["composition_checks"] == ["business"]
    from feasibility_scheduler.adapters.acceptance_profile import validate_responsibilities
    with pytest.raises(ValueError, match="no executable checker"):
        validate_responsibilities(plan, tasks, checkers={})
    plan["acceptance_assignments"][0].update(scope="local", owners=["a"])
    with pytest.raises(ValueError, match="must run at root"):
        validate_plan(plan, tasks)


def test_split_merge_preserves_obligations_without_fixed_task_count():
    tasks, plan = hierarchical_fixture()
    plan["modules"][-1]["execution_dependencies"] = []
    p = {"id": "artifacts", "categories": [{"id": "delivery", "description": "Deliver artifacts",
         "applicable": True, "allowed_kinds": ["file_exists"], "checker_names": []}]}
    plan.update(acceptance_profile=p, acceptance_assignments=[
        {"id": "deliver", "category": "delivery", "requirements": ["R1"],
         "owners": ["root"], "checks": [c["id"] for c in plan["checks"]], "scope": "local"}])
    tasks = [replace(t, payload={**t.payload, "acceptance_profile": p}) for t in tasks]
    original = copy.deepcopy(plan)
    attached = attach_plan(tasks, plan)
    split = next(c for c in generate_candidates(attached, states={}) if c.candidate_id == "contract-split:root")
    assert len(split.tasks) == 2
    for child in split.tasks:
        assert child.payload["contract_plan"] == original
        assert child.payload["execution_contract"]["acceptance_responsibilities"] == original["acceptance_assignments"]
    merged = next(c for c in generate_candidates(split.tasks, states={}) if c.kind == "merge")
    assert len(merged.tasks) == 1
    assert merged.tasks[0].payload["execution_contract"]["acceptance_responsibilities"] == original["acceptance_assignments"]
    assert plan == original


def test_na_requires_host_reason():
    p = profile()
    del p["categories"][-1]["reason"]
    with pytest.raises(ValueError, match="N/A decision requires a reason"):
        validate_profile(p)


def test_real_cli_enables_profile_and_stops_without_paid_model_call(tmp_path):
    _, _, _, cfg = setup_task(tmp_path)
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, str(root / "scripts/run_legacy_doc_task.py"),
                           "--client", "http", "--task-input", str(cfg.task_input_path),
                           "--task-md", str(cfg.task_md_path), "--workdir", str(cfg.workdir)],
                          cwd=root, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 2
    assert "no executable checker registered" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not (cfg.workdir / "events.jsonl").exists()
    assert json.loads((cfg.workdir / "acceptance-preflight.json").read_text())["profile_id"] == "software-v1"


def test_transactional_example_has_twelve_executable_obligations():
    path = Path(__file__).resolve().parents[1] / "docs/acceptance-profiles/transactional-resource-v1.json"
    p = json.loads(path.read_text())
    validate_profile(p, {"task-acceptance": lambda _: {"status": "passed"}})
    assert len(p["categories"]) == 12


@pytest.mark.parametrize("same_total", [False, True])
def test_default_profile_uses_actual_root_checker_and_cannot_accept_local_passes(tmp_path, monkeypatch, same_total):
    _, plan, data, cfg = setup_task(tmp_path)
    p = software_profile()
    plan["checks"] = [c for c in plan["checks"] if c["kind"] == "file_exists"] + [
        {"id": "suite", "kind": "external", "name": "task-acceptance", "paths": ["a.json"],
         "requirements": ["R1", "R2"]}]
    plan["acceptance_assignments"] = [
        {"id": c["id"], "category": c["id"], "requirements": ["R1", "R2"],
         "owners": [ROOT], "checks": ["suite"], "scope": "root"} for c in p["categories"]]
    plan["acceptance_assignments"].append({"id": "delivery", "category": "artifacts", "requirements": ["R2"],
         "owners": [ROOT], "checks": ["a-exists", "b-exists"], "scope": "root"})
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    called = []
    def checker(workspace):
        called.append(workspace)
        a = json.loads((workspace / "a.json").read_text())["total"]
        b = json.loads((workspace / "b.json").read_text())["total"]
        return {"status": "passed" if a == b else "failed", "detail": "Compare actual delivered totals"}
    client = FakeClient([step({"a.json": '{"total":1}'}),
                         step({"b.json": json.dumps({"total": 1 if same_total else 2})})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, acceptance_profile=p, external_checkers={"task-acceptance": checker},
                                   contract_repair_limit=0))
    assert outcome.ok is same_total
    assert outcome.reports["a"].passed and outcome.reports["b"].passed
    assert outcome.reports[ROOT].passed is same_total
    assert len(called) == 1  # Never run a workspace suite on a partially delivered project.
