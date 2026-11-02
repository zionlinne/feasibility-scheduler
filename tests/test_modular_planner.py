import json
from dataclasses import replace

import pytest

from feasibility_scheduler.adapters.contract_plan import attach_plan, generate_plan, validate_plan
from feasibility_scheduler.adapters.graph_rewrite import generate_candidates, recover_failed_merge
from feasibility_scheduler.adapters.legacy_doc import build_legacy_tasks, run_legacy_doc
from feasibility_scheduler.adapters.modular_standard import ownership_catalog
from feasibility_scheduler.fake_client import FakeClient, Step
from test_contract_plan import fixture, hierarchical_fixture
from test_contract_runtime import setup_task, step


def standardize(tasks, plan):
    plan["planning_standard"] = "modular-v1"
    owned = ownership_catalog(tasks, plan)
    for module in plan["modules"]:
        module.update(owned_files=owned[module["id"]], boundary="One functional responsibility",
                      invariants=["Preserve consistent data"], estimated_tokens=400,
                      local_checks=[c["id"] for c in plan["checks"]
                                    if c["kind"] == "file_exists" and c["path"] in owned[module["id"]]],
                      acceptance_limitations="Existence is structural evidence only; full behavior requires root acceptance")
    return tasks, plan


def functional_fixture():
    return standardize(*hierarchical_fixture())


def test_new_plan_is_host_bound_and_keeps_functional_groups():
    tasks, plan = functional_fixture()
    tasks = [replace(t, payload={**t.payload, "planning_standard": "modular-v1"}) for t in tasks]
    del plan["planning_standard"]  # Not transcribed by the worker.
    client = FakeClient([Step.success(body=json.dumps(plan))])
    requests = []
    original = client.call
    client.call = lambda request: requests.append(request) or original(request)
    result = generate_plan(client, tasks, "Compare offers")
    assert result["planning_standard"] == "modular-v1"
    assert "Keeping the task whole is valid" in requests[0].prompt
    assert "never divide a parent estimate" in requests[0].prompt
    attached = attach_plan(tasks, result)
    candidates = generate_candidates(attached, states={})
    assert not any(c.candidate_id == "split:root" for c in candidates)
    selected = next(c for c in candidates if c.candidate_id == "contract-split:root")
    first = next(t for t in selected.tasks if t.id == "first")
    assert set(first.payload["owned_files"]) == {"a.py", "b.py"}
    assert not any(c.kind == "split" for c in generate_candidates(selected.tasks, states={}))
    assert first.payload["estimated_tokens"] == 400


@pytest.mark.parametrize("fault", ["missing-child", "boundary", "invariants", "estimate", "boolean-estimate",
                                    "ownership", "empty-checks", "foreign-check", "limitations", "group"])
def test_invalid_functional_boundary_is_rejected(fault):
    tasks, plan = functional_fixture()
    module = plan["modules"][1]
    if fault == "missing-child":
        plan["modules"].pop()
    elif fault == "boundary":
        module["boundary"] = ""
    elif fault == "invariants":
        module["invariants"] = [""]
    elif fault == "estimate":
        module["estimated_tokens"] = 0
    elif fault == "boolean-estimate":
        module["estimated_tokens"] = True
    elif fault == "ownership":
        module["owned_files"] = ["c.py"]
    elif fault == "empty-checks":
        module["local_checks"] = []
    elif fault == "foreign-check":
        module["local_checks"] = ["c.py"]
    elif fault == "limitations":
        del module["acceptance_limitations"]
    else:
        module["cohesion_group"] = ""
    with pytest.raises(ValueError):
        validate_plan(plan, tasks)


def test_independent_functional_modules_are_not_arbitrarily_merged():
    tasks, plan = standardize(*fixture())
    attached = attach_plan(tasks, plan)
    assert not any(c.kind == "merge" for c in generate_candidates(attached, states={}))
    assert not any(c.kind == "split" for c in generate_candidates(attached, states={}))


@pytest.mark.parametrize("functional", [False, True])
def test_every_candidate_keeps_benefit_over_cost_formula(functional):
    tasks, plan = functional_fixture() if functional else fixture()
    for candidate in generate_candidates(attach_plan(tasks, plan), states={}):
        assert candidate.details["estimated_cost"] > 0
        assert candidate.score == candidate.details["estimated_benefit"] / candidate.details["estimated_cost"]


def test_explicit_cohesion_merge_recovers_original_functional_boundaries():
    tasks, plan = standardize(*fixture())
    for module in plan["modules"]:
        module["cohesion_group"] = "one-shared-invariant"
    attached = attach_plan(tasks, plan)
    merge = next(c for c in generate_candidates(attached, states={}) if c.kind == "merge")
    merged = merge.tasks[0]
    assert not any(c.kind == "split" for c in generate_candidates(merge.tasks, states={}))
    recovered = recover_failed_merge(merge.tasks, merged)
    assert recovered is not None
    units = {frozenset(t.payload["owned_files"]) for t in recovered.tasks}
    assert units == {frozenset({"a.py", "b.py"}), frozenset({"c.py"})}
    for task in recovered.tasks:
        assert len(task.payload["execution_contract"]["module_contracts"]) == 1
    assert not any(c.kind == "merge" for c in generate_candidates(recovered.tasks, states={}))


def test_legacy_merge_split_recompiles_scope_without_parent_instructions():
    tasks, plan = fixture()
    tasks = [replace(t, payload={**t.payload, "prompt_text": "OLD ROOT DELIVERY INSTRUCTIONS",
                                "task_source": "The complete original specification.", "estimated_tokens": 500}) for t in tasks]
    attached = attach_plan(tasks, plan)
    merge = next(c for c in generate_candidates(attached, states={}) if c.kind == "merge")
    split = next(c for c in generate_candidates(merge.tasks, states={}) if c.kind == "split")
    for task in split.tasks:
        prompt = task.payload["prompt_text"]
        assert "OLD ROOT DELIVERY" not in prompt and "REWRITE-MERGE" not in prompt and "REWRITE-SPLIT" not in prompt
        delivery = json.loads(prompt.split("CURRENT DELIVERY CONTRACT\n")[1].split("\nDeliver ALL")[0])
        assert delivery["task_id"] == task.id
        assert set(delivery["owned_files"]) == set(task.payload["owned_files"])
        assert set(delivery["response"]["files"]) == set(task.payload["owned_files"])
        expected = "a" if set(task.payload["owned_files"]) <= {"a.py", "b.py"} else "b"
        assert task.payload["contract_module_ids"] == [expected]
        assert [m["id"] for m in task.payload["execution_contract"]["module_contracts"]] == [expected]
        assert prompt.count("The complete original specification.") == 1
        assert "manifest-only" in prompt


def test_merge_preserves_both_original_sources_and_split_projects_them():
    tasks, plan = fixture()
    tasks = [replace(t, payload={**t.payload, "task_source": f"Specification for {t.id}", "estimated_tokens": 500}) for t in tasks]
    merge = next(c for c in generate_candidates(attach_plan(tasks, plan), states={}) if c.kind == "merge")
    assert "Specification for a" in merge.tasks[0].payload["prompt_text"]
    assert "Specification for b" in merge.tasks[0].payload["prompt_text"]
    split = next(c for c in generate_candidates(merge.tasks, states={}) if c.kind == "split")
    for task in split.tasks:
        mids = task.payload["contract_module_ids"]
        assert len(mids) == 1
        assert task.payload["task_source"] == f"Specification for {mids[0]}"


def test_subset_repair_prompt_has_only_its_current_write_scope():
    from feasibility_scheduler.adapters.work_unit import compile_repair_prompt
    tasks, plan = fixture()
    task = attach_plan(tasks, plan)[0]
    prompt = compile_repair_prompt(task, ["a.py"], {"errors": ["bad a"], "read_only_artifacts": {"b.py": "peer"}})
    delivery = json.loads(prompt.split("CURRENT DELIVERY CONTRACT\n")[1].split("\nDeliver ALL")[0])
    assert delivery["owned_files"] == ["a.py"]
    assert set(delivery["response"]["files"]) == {"a.py"}
    assert task.payload["owned_files"] == ["a.py", "b.py"]


def test_legacy_proposal_does_not_invent_smaller_output_cost_by_division():
    tasks, plan = hierarchical_fixture()
    tasks = [replace(tasks[0], payload={**tasks[0].payload, "estimated_tokens": 12000})]
    # Automatic legacy per-file split may still exist; the explicit proposal retains a conservative estimate.
    split = next(c for c in generate_candidates(attach_plan(tasks, plan), states={}) if c.candidate_id == "contract-split:root")
    assert all(t.payload["estimated_tokens"] == 12000 for t in split.tasks)
    assert all(t.payload["request_cost"]["prompt_utf8_bytes"] > 0 for t in split.tasks)


def test_declared_upstream_cross_check_runs_before_root(tmp_path, monkeypatch):
    _, plan, data, cfg = setup_task(tmp_path)
    plan["modules"][1]["execution_dependencies"] = ["a"]
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([step({"a.json": '{"total":1}'}), step({"b.json": '{"total":2}'})])
    requests = []
    send = client.acall

    async def capture(request):
        requests.append(request)
        return await send(request)

    client.acall = capture
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, contract_repair_limit=0))
    assert not outcome.ok and outcome.verdict.trace_complete
    assert outcome.states["b"].value == "FAILED"
    assert any("same-total" in error for error in outcome.reports["b"].errors)
    assert outcome.states["__contract_composition__"].value == "PENDING"
    assert 'CURRENT UPSTREAM ARTIFACTS' in requests[1].prompt
    assert '\\"total\\":1' in requests[1].prompt
    assert requests[1].metadata["request_cost"]["prompt_utf8_bytes"] == len(requests[1].prompt.encode())


def test_modular_node_workflow_with_multifile_unit_and_integration(tmp_path, monkeypatch):
    import shutil
    from feasibility_scheduler.adapters.legacy_doc import LegacyDocConfig
    from feasibility_scheduler.adapters.contract_checks import requirement_ledger

    if not shutil.which("node"):
        pytest.skip("Node unavailable")
    source = "Use existing business modules through the entry point.\nReturn 42."
    owned = ["domain.mjs", "math.mjs", "entry.mjs"]
    data = {"modules": [{"module_id": "root", "owned_files": owned, "estimated_tokens": 12000}]}
    tasks = build_legacy_tasks(data, task_md=source)
    declaration = {"id": "root", "precondition": "shared interfaces", "postcondition": "application returns 42",
                   "handoff": "domain.total() uses math.base()", "verification": "Node black box",
                   "execution_dependencies": [], "interface_dependencies": []}
    plan = {"version": 2, "unresolved": [], "requirements": requirement_ledger(source),
            "root": {"precondition": "Node", "postcondition": "returns 42", "verification": "Node black box"},
            "composition": {"method": "wire entry to domain", "consistency": "one business implementation",
                            "verification": "run entry", "counterexample_review": "unused domain must fail"},
            "modules": [declaration, {**declaration, "id": "business"},
                        {**declaration, "id": "entry", "execution_dependencies": ["business"], "interface_dependencies": ["business"]}],
            "decompositions": [{"parent": "root", "children": [
                {"id": "business", "owned_files": owned[:2], "postcondition": "one cohesive business module across two files"},
                {"id": "entry", "owned_files": owned[2:], "postcondition": "call the actual business module"}]}],
            "checks": [{"id": p, "kind": "file_exists", "path": p, "requirements": ["R1", "R2"]} for p in owned]
                      + [{"id": "business-suite", "kind": "external", "name": "task-acceptance", "read_paths": owned,
                          "repair_paths": ["entry.mjs"], "requirements": ["R1", "R2"]}]}
    standardize(tasks, plan)
    data["contract_plan"] = plan
    input_path, task_path = tmp_path / "input.json", tmp_path / "TASK.md"
    input_path.write_text(json.dumps(data), encoding="utf-8")
    task_path.write_text(source, encoding="utf-8")
    verifier = tmp_path / "verify.mjs"
    verifier.write_text("""import fs from 'node:fs'; import {execFileSync} from 'node:child_process';
const [solution,report]=process.argv.slice(2);
const actual=execFileSync(process.execPath,['entry.mjs'],{cwd:solution,encoding:'utf8'}).trim();
const passed=actual==='42';
fs.writeFileSync(report,JSON.stringify({solution,summary:{passed:passed?1:0,failed:passed?0:1,error:0},cases:[]}));
""", encoding="utf-8")
    domain = 'import {base} from "./math.mjs"; export function total(){return base()+2;}\n'
    math = 'export function base(){return 40;}\n'
    wired = 'import {total} from "./domain.mjs"; console.log(total());\n'
    client = FakeClient([step({"domain.mjs": domain, "math.mjs": math}),
                         step({"entry.mjs": 'console.log("unwired");'}), step({"entry.mjs": wired})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    cfg = LegacyDocConfig(input_path, task_path, tmp_path / "run", client_kind="http", policy="rewrite",
                          acceptance="node-check", acceptance_script=verifier, integration_files=("entry.mjs",),
                          contract_repair_limit=0)
    outcome = run_legacy_doc(cfg)
    assert outcome.ok and outcome.verdict.trace_complete
    assert set(outcome.states) == {"business", "entry", "__contract_integration__", "__contract_composition__"}
    assert len(client.responses) == 3  # All scripted; no planner/model/network call.
    assert (cfg.workdir / "workspace/domain.mjs").read_text(encoding="utf-8") == domain
    assert (cfg.workdir / "workspace/math.mjs").read_text(encoding="utf-8") == math
    evidence = json.loads(next(cfg.workdir.glob("module-execution-*.json")).read_text(encoding="utf-8"))
    assert not evidence["not_loaded"] and not evidence["no_function_calls"]


def test_new_real_entry_requests_modular_standard_offline(tmp_path, monkeypatch):
    tasks, plan, data, cfg = setup_task(tmp_path)
    standardize(tasks, plan)
    del data["contract_plan"]
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([Step.success(body=json.dumps(plan)),
                         step({"a.json": '{"total":1}'}), step({"b.json": '{"total":1}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, client_kind="http", policy="rewrite"))
    assert outcome.ok and outcome.verdict.trace_complete
    assert json.loads((cfg.workdir / "contract.json").read_text())["planning_standard"] == "modular-v1"
    assert not any(row["kind"] == "merge" for row in json.loads((cfg.workdir / "summary.json").read_text(encoding="utf-8"))["rewrite_history"])


@pytest.mark.parametrize("body", [
    {"ok": True, "delivery": "manifest", "manifest": ["a.json", "b.json"]},
    {"ok": True, "files": {"a.json": '{"total":1}'}},
    {"ok": True, "files": {"a.json": '{"total":1}', "b.json": '{"total":1}', "other.json": "bad"}},
])
def test_functional_unit_cannot_pass_with_manifest_partial_or_extra_delivery(tmp_path, monkeypatch, body):
    _, plan, data, cfg = setup_task(tmp_path)
    data["modules"] = [{"module_id": "root", "owned_files": ["a.json", "b.json"]}]
    plan["modules"] = [{**plan["modules"][0], "id": "root"}]
    tasks = build_legacy_tasks(data, task_md=cfg.task_md_path.read_text(encoding="utf-8"))
    standardize(tasks, plan)
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    workspace = cfg.workdir / "workspace"
    workspace.mkdir(parents=True)
    for path in ("a.json", "b.json"):
        (workspace / path).write_text('{"total":1}', encoding="utf-8")
    client = FakeClient([Step.success(body=json.dumps(body))])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, policy="rewrite", contract_repair_limit=0))
    assert not outcome.ok and outcome.verdict.trace_complete
    assert outcome.states["root"].value == "FAILED"
    assert not (workspace / "other.json").exists()
    assert len(client.responses) == 1
