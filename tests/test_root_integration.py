import json
import subprocess
from dataclasses import replace

import pytest

from feasibility_scheduler.adapters.contract_checks import evaluate_checks
from feasibility_scheduler.adapters.legacy_doc import run_legacy_doc
from feasibility_scheduler.fake_client import FakeClient
from test_contract_runtime import setup_task, step


def pair(tmp_path, *, aliases=2):
    _, plan, data, cfg = setup_task(tmp_path)
    data["modules"][0]["owned_files"] = ["consumer.mjs"]
    data["modules"][1]["owned_files"] = ["provider.mjs"]
    plan["checks"] = [
        {"id": "consumer", "kind": "file_exists", "path": "consumer.mjs", "requirements": ["R2"]},
        {"id": "provider", "kind": "file_exists", "path": "provider.mjs", "requirements": ["R2"]},
    ] + [{"id": f"integration-{i}", "kind": "external", "name": "suite", "requirements": ["R1"],
          "read_paths": ["consumer.mjs", "provider.mjs"], "repair_paths": ["consumer.mjs"]} for i in range(aliases)]
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    return replace(cfg, acceptance="node-check")


@pytest.mark.parametrize("provider,initial,fixed,expected", [
    ("export function supplied(x){return x+1}",
     "import {missing} from './provider.mjs'; export function compute(){return missing(2)}",
     "import {supplied} from './provider.mjs'; export function compute(){return supplied(2)}", "3"),
    ("export function supplied(left,right){return left-right}",
     "import {supplied} from './provider.mjs'; export function compute(){return supplied(2,9)}",
     "import {supplied} from './provider.mjs'; export function compute(){return supplied(9,2)}", "7"),
    ("export function supplied(state){return state.rows.get('key')}",
     "import {supplied} from './provider.mjs'; export function compute(){return supplied({rows:{key:7}})}",
     "import {supplied} from './provider.mjs'; export function compute(){return supplied({rows:new Map([['key',7]])})}", "7"),
], ids=["missing-export", "argument-order", "state-shape"])
def test_real_node_integration_failure_repair_and_readonly_context(tmp_path, monkeypatch, provider, initial, fixed, expected):
    cfg = pair(tmp_path)
    requests, suite_calls = [], []
    class RecordingClient(FakeClient):
        async def acall(self, request):
            requests.append(request)
            return await super().acall(request)
    client = RecordingClient([step({"consumer.mjs": initial}), step({"provider.mjs": provider}),
                              step({"consumer.mjs": fixed})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    def checker(workspace):
        suite_calls.append(workspace)
        process = subprocess.run(["node", "--input-type=module", "--eval",
                                  "import {compute} from './consumer.mjs'; console.log(compute())"],
                                 cwd=workspace, capture_output=True, text=True, timeout=5)
        passed = process.returncode == 0 and process.stdout.strip() == expected
        return {"status": "passed" if passed else "failed", "detail": process.stderr or process.stdout}
    outcome = run_legacy_doc(replace(cfg, external_checkers={"suite": checker}))
    assert outcome.ok and outcome.verdict.trace_complete
    assert outcome.reports["a"].passed and outcome.reports["b"].passed
    assert len(suite_calls) == 2  # Once before repair, once after; not per alias.
    feedback = json.loads(next(cfg.workdir.glob("contract-repair-*.json")).read_text())
    assert feedback["read_only_artifacts"] == {"provider.mjs": provider}
    assert feedback["repair_files"] == ["consumer.mjs"]
    assert feedback["artifacts"] == {"consumer.mjs": initial}
    repair = next(r for r in requests if r.task_id == "__contract_composition__")
    assert provider in repair.prompt
    assert tuple(repair.metadata["owned_files"]) == ("consumer.mjs",)
    assert (cfg.workdir / "workspace/provider.mjs").read_text() == provider
    reports = [json.loads(p.read_text()) for p in cfg.workdir.glob("contract-checks-*.json")]
    root_reports = [r for r in reports if r["task_id"] == "__contract_composition__"]
    before, after = [[r for r in report["results"] if r["id"].startswith("integration-")] for report in root_reports]
    assert [r["status"] for r in before] == ["failed", "failed"]
    assert [r["status"] for r in after] == ["passed", "passed"]
    assert after[0]["execution"] == "executed" and after[1]["execution"] == "reused"
    assert before[0]["workspace_version"] != after[0]["workspace_version"]


def test_root_syntax_regression_cannot_pass_incomplete_business_check(tmp_path, monkeypatch):
    cfg = pair(tmp_path)
    client = FakeClient([step({"consumer.mjs": "export const value = 0;"}),
                         step({"provider.mjs": "export const provider = 1;"}),
                         step({"consumer.mjs": "export const value = ; // FIXED"})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    called = []
    def incomplete_checker(workspace):
        called.append(workspace)
        return {"status": "passed" if "FIXED" in (workspace / "consumer.mjs").read_text() else "failed"}
    outcome = run_legacy_doc(replace(cfg, external_checkers={"suite": incomplete_checker}))
    assert not outcome.ok
    assert outcome.reports["a"].passed and outcome.reports["b"].passed
    assert any("node --check" in error for error in outcome.reports["__contract_composition__"].errors)
    assert len(called) == 1  # Invalid repaired syntax blocks another business-suite run.


@pytest.mark.parametrize("status", ["passed", "failed", "unverified"])
def test_same_version_checker_aliases_reuse_all_verdicts(tmp_path, status):
    (tmp_path / "artifact.json").write_text("{}")
    plan = {"checks": [{"id": str(i), "kind": "external", "name": "suite", "requirements": ["R1"],
                         "paths": ["artifact.json"]} for i in range(20)]}
    calls = []
    def check(workspace):
        calls.append(workspace)
        return {"status": status, "detail": "same actual execution"}
    results = evaluate_checks(plan, tmp_path, external_checkers={"suite": check})
    assert len(calls) == 1 and len(results) == 20
    assert {r["status"] for r in results} == {status}
    assert sum(r["execution"] == "executed" for r in results) == 1
    assert sum(r["execution"] == "reused" for r in results) == 19


def test_checker_exception_is_unverified_and_not_repeated_per_alias(tmp_path):
    plan = {"checks": [{"id": str(i), "kind": "external", "name": "suite", "requirements": ["R1"],
                         "paths": ["artifact.json"]} for i in range(2)]}
    calls = []
    def timeout(workspace):
        calls.append(workspace)
        raise TimeoutError("real checker timed out")
    results = evaluate_checks(plan, tmp_path, external_checkers={"suite": timeout})
    assert len(calls) == 1 and all(r["status"] == "unverified" for r in results)


def test_checker_mutation_invalidates_pass_and_repairs_never_reuse_old_round(tmp_path):
    target = tmp_path / "artifact.json"
    target.write_text("old")
    plan = {"checks": [{"id": "one", "kind": "external", "name": "suite", "requirements": ["R1"],
                         "paths": ["artifact.json"]}]}
    def mutating(workspace):
        target.write_text("changed")
        return {"status": "passed"}
    result = evaluate_checks(plan, tmp_path, external_checkers={"suite": mutating})[0]
    assert result["status"] == "unverified" and "changed observed artifacts" in result["detail"]
    calls = []
    def checker(workspace):
        calls.append(target.read_text())
        return {"status": "passed"}
    first = evaluate_checks(plan, tmp_path, external_checkers={"suite": checker})[0]
    target.write_text("repaired")
    second = evaluate_checks(plan, tmp_path, external_checkers={"suite": checker})[0]
    assert calls == ["changed", "repaired"]
    assert first["workspace_version"] != second["workspace_version"]
