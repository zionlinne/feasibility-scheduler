import json
from dataclasses import replace

import pytest

from feasibility_scheduler.adapters.integration import integration_task
from feasibility_scheduler.adapters.legacy_doc import run_legacy_doc
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.statemachine import TaskState
from test_contract_runtime import setup_task, step


def run_stage(tmp_path, monkeypatch, result, *, policy="fixed"):
    _, _, _, cfg = setup_task(tmp_path)
    client = FakeClient([step({"a.json": '{"total":1}'}), step({"b.json": '{"total":2}'}), result])
    requests = []
    send = client.acall

    async def capture(request):
        requests.append(request)
        return await send(request)

    client.acall = capture
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, policy=policy, integration_files=("b.json",)))
    return cfg, outcome, requests


@pytest.mark.parametrize("policy", ["fixed", "rewrite"])
def test_integration_uses_delivered_sources_then_independent_root(tmp_path, monkeypatch, policy):
    cfg, outcome, requests = run_stage(tmp_path, monkeypatch, step({"b.json": '{"total":1}'}), policy=policy)
    assert outcome.ok and outcome.verdict.trace_complete
    assert outcome.states["__contract_integration__"] == TaskState.ACCEPTED
    assert outcome.states["__contract_composition__"] == TaskState.ACCEPTED
    assert len(requests) == 3
    request = requests[-1]
    assert tuple(request.metadata["owned_files"]) == ("b.json",)
    assert request.metadata["allow_format_repair"] is False
    context = json.loads((cfg.workdir / "integration-context.json").read_text())
    assert context["editable_artifacts"] == {"b.json": '{"total":2}'}
    assert context["read_only_artifacts"] == {"a.json": '{"total":1}'}
    assert set(context["sha256"]) == {"a.json", "b.json"}
    assert not list(cfg.workdir.glob("contract-repair-*.json"))


def test_root_rejects_bad_integration_without_extra_model_repair(tmp_path, monkeypatch):
    _, outcome, requests = run_stage(tmp_path, monkeypatch, step({"b.json": '{"total":3}'}))
    assert not outcome.ok
    assert outcome.states["__contract_integration__"] == TaskState.ACCEPTED
    assert outcome.states["__contract_composition__"] == TaskState.FAILED
    assert len(requests) == 3


def test_readonly_write_is_rejected_before_any_mutation(tmp_path, monkeypatch):
    cfg, outcome, requests = run_stage(tmp_path, monkeypatch,
                                       step({"a.json": "changed", "b.json": "changed"}))
    assert outcome.states["__contract_integration__"] == TaskState.FAILED
    assert outcome.states["__contract_composition__"] != TaskState.ACCEPTED
    assert (cfg.workdir / "workspace/a.json").read_text() == '{"total":1}'
    assert (cfg.workdir / "workspace/b.json").read_text() == '{"total":2}'
    assert len(requests) == 3


@pytest.mark.parametrize("body", [
    {"ok": True, "delivery": "manifest", "manifest": ["b.json"], "files": {"b.json": "changed"}},
    {"ok": False, "files": {"b.json": "changed"}},
    {"ok": True, "files": {}},
])
def test_invalid_envelope_preserves_existing_outputs(tmp_path, monkeypatch, body):
    cfg, outcome, requests = run_stage(tmp_path, monkeypatch, Step.success(body=json.dumps(body)))
    assert outcome.states["__contract_integration__"] == TaskState.FAILED
    assert (cfg.workdir / "workspace/b.json").read_text() == '{"total":2}'
    assert len(requests) == 3


@pytest.mark.parametrize("failure,expected", [
    (Step.timeout(), TaskState.RECOVERY_REQUIRED),
    (Step.server_error(), TaskState.FAILED),
    (Step.invalid_json(), TaskState.FAILED),
])
def test_integration_failure_is_one_shot(tmp_path, monkeypatch, failure, expected):
    _, outcome, requests = run_stage(tmp_path, monkeypatch, failure)
    assert not outcome.ok
    assert outcome.states["__contract_integration__"] == expected
    assert len(requests) == 3


@pytest.mark.parametrize("scope", [[], ["../a.json"], ["a.json", "a.json"], ["missing.json"], ["./a.json"], "a.json"])
def test_host_scope_preflight(tmp_path, scope):
    tasks, plan, _, _ = setup_task(tmp_path)
    with pytest.raises(ValueError, match="integration_files"):
        integration_task(tasks, plan, scope)


def test_rewrite_keeps_integration_barrier_when_producer_splits(tmp_path):
    from feasibility_scheduler.adapters.contract_plan import attach_plan
    from feasibility_scheduler.adapters.contract_runtime import composition_task
    from feasibility_scheduler.adapters.graph_rewrite import generate_candidates

    tasks, plan, _, _ = setup_task(tmp_path)
    parent = replace(tasks[0], payload={**tasks[0].payload, "owned_files": ["a.json", "b.json"]})
    plan["modules"] = [plan["modules"][0]]
    plan["decompositions"] = [{"parent": "a", "children": [
        {"id": "one", "owned_files": ["a.json"], "postcondition": "first offer"},
        {"id": "two", "owned_files": ["b.json"], "postcondition": "second offer"}]}]
    leaves = attach_plan([parent], plan)
    stage = integration_task(leaves, plan, ["b.json"])
    root = composition_task(leaves, plan)
    root = replace(root, dependencies=root.dependencies + (stage.id,))
    split = next(c for c in generate_candidates(leaves + [stage, root], states={})
                 if c.candidate_id == "contract-split:a")
    stage_after = next(t for t in split.tasks if t.id == stage.id)
    assert set(stage_after.dependencies) == {"one", "two"}
    root_after = next(t for t in split.tasks if t.id == root.id)
    assert stage.id in root_after.dependencies
    assert stage_after.payload["integration_files"] == ["b.json"]


def test_http_integration_does_not_make_format_repair_request(tmp_path):
    from feasibility_scheduler.fake_client import ApiRequest, Outcome
    from feasibility_scheduler.http_client import HttpClient
    from test_http_client import FakeHTTPResponse, openai_success

    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        return FakeHTTPResponse(json.dumps(openai_success(content='{"ok":true,"files":{"b.json":')))

    client = HttpClient(api_key="unit-test-key", opener=opener, raw_dir=tmp_path)
    response = client.call(ApiRequest(task_id="__contract_integration__", idempotency_key="integration-1",
                                     prompt="Integrate", metadata={"artifact_delivery": True,
                                                                   "owned_files": ["b.json"],
                                                                   "allow_format_repair": False}))
    assert response.outcome is Outcome.INVALID_JSON
    assert len(calls) == 1
    assert client.repair_prompts == []


def test_real_node_entrypoint_calls_unchanged_leaf(tmp_path, monkeypatch):
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("Node unavailable")
    _, plan, data, cfg = setup_task(tmp_path)
    data["modules"][0]["owned_files"] = ["domain.mjs"]
    data["modules"][1]["owned_files"] = ["entry.mjs"]
    plan["checks"] = [
        {"id": "domain", "kind": "file_exists", "path": "domain.mjs", "requirements": ["R2"]},
        {"id": "entry", "kind": "file_exists", "path": "entry.mjs", "requirements": ["R2"]},
        {"id": "run", "kind": "external", "name": "node-entry", "scope": "root",
         "read_paths": ["domain.mjs", "entry.mjs"], "repair_paths": ["entry.mjs"], "requirements": ["R1"]},
    ]
    data["contract_plan"] = plan
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    leaf = "export function total() { return 42; }\n"
    connected = 'import {total} from "./domain.mjs"; console.log(total());\n'
    client = FakeClient([step({"domain.mjs": leaf}), step({"entry.mjs": 'console.log("not connected");'}),
                         step({"entry.mjs": connected})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    observed = []

    def checker(workspace):
        result = subprocess.run(["node", "entry.mjs"], cwd=workspace, capture_output=True, text=True, timeout=10)
        observed.append(result.stdout.strip())
        return {"status": "passed" if result.returncode == 0 and result.stdout.strip() == "42" else "failed",
                "detail": result.stdout + result.stderr}

    outcome = run_legacy_doc(replace(cfg, integration_files=("entry.mjs",), acceptance="node-check",
                                     external_checkers={"node-entry": checker}))
    assert outcome.ok and outcome.verdict.trace_complete
    assert observed == ["42"]
    assert (cfg.workdir / "workspace/domain.mjs").read_text() == leaf
    assert (cfg.workdir / "workspace/entry.mjs").read_text() == connected
