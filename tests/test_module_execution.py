import json
from pathlib import Path

import pytest
from dataclasses import replace

from feasibility_scheduler.adapters.external_verifier import node_verifier
from feasibility_scheduler.adapters.legacy_doc import run_legacy_doc
from feasibility_scheduler.fake_client import FakeClient
from test_contract_runtime import setup_task, step


def verifier(tmp_path):
    path = tmp_path / "verify.mjs"
    path.write_text("""import fs from 'node:fs'; import {spawnSync} from 'node:child_process';
const [solution,report]=process.argv.slice(2);
const result=spawnSync(process.execPath,['main.mjs'],{cwd:solution,encoding:'utf8'});
const ok=result.status===0&&result.stdout.trim()==='6';
fs.writeFileSync(report,JSON.stringify({solution,summary:{passed:ok?1:0,failed:ok?0:1,error:0},cases:[]}));
process.exitCode=ok?0:1;
""", encoding="utf-8")
    return path


@pytest.mark.parametrize("main,status,loaded", [
    ("console.log(6)", "failed", False),
    ("import './domain.mjs'; console.log(6)", "failed", True),
    ("import {compute} from './domain.mjs'; console.log(compute())", "passed", True),
])
def test_business_pass_requires_actual_domain_execution(tmp_path, main, status, loaded):
    workspace = tmp_path / "solution"
    workspace.mkdir()
    (workspace / "main.mjs").write_text(main)
    (workspace / "domain.mjs").write_text("export function compute(){return 6}")
    check = node_verifier(verifier(tmp_path), tmp_path / "reports",
                          required_execution_files=["main.mjs", "domain.mjs"])
    result = check(workspace)
    assert result["status"] == status
    detail = json.loads(result["detail"])
    assert detail["summary"]["passed"] == 1  # Business report alone cannot override unused modules.
    assert detail["module_execution"]["files"]["domain.mjs"]["loaded"] is loaded
    if status == "passed":
        assert detail["module_execution"]["files"]["domain.mjs"]["function_called"]
    elif loaded:
        assert "domain.mjs" in detail["module_execution"]["no_function_calls"]
    else:
        assert "domain.mjs" in detail["module_execution"]["not_loaded"]


def test_constant_only_modules_need_load_not_fabricated_function_calls(tmp_path):
    workspace = tmp_path / "solution"
    workspace.mkdir()
    (workspace / "main.mjs").write_text("import {value} from './domain.mjs';console.log(value)")
    (workspace / "domain.mjs").write_text("export const value=6")
    result = node_verifier(verifier(tmp_path), tmp_path / "reports",
                           required_execution_files=["main.mjs", "domain.mjs"])(workspace)
    assert result["status"] == "passed"


def test_http_response_flushes_coverage_before_forced_server_stop(tmp_path):
    workspace = tmp_path / "solution"
    workspace.mkdir()
    (workspace / "domain.mjs").write_text("export function compute(){return 6}")
    (workspace / "server.mjs").write_text("""import http from 'node:http';import {compute} from './domain.mjs';
const server=http.createServer((req,res)=>{res.end(String(compute()))});
server.listen(0,'127.0.0.1',()=>console.log(server.address().port));
""")
    script = tmp_path / "verify-http.mjs"
    script.write_text("""import fs from 'node:fs';import {spawn} from 'node:child_process';
const [solution,report]=process.argv.slice(2);
const child=spawn(process.execPath,['server.mjs'],{cwd:solution,stdio:['ignore','pipe','pipe']});
let output='';
const port=await new Promise((resolve,reject)=>{child.stdout.on('data',chunk=>{output+=chunk;if(output.includes('\\n'))resolve(Number(output.trim()))});child.once('error',reject)});
const text=await (await fetch('http://127.0.0.1:'+port)).text();
const stopped=new Promise(resolve=>child.once('exit',resolve));child.kill();await stopped;
const ok=text==='6';fs.writeFileSync(report,JSON.stringify({solution,summary:{passed:ok?1:0,failed:ok?0:1,error:0},cases:[]}));
""")
    result = node_verifier(script, tmp_path / "reports", timeout_seconds=15,
                           required_execution_files=["server.mjs", "domain.mjs"])(workspace)
    assert result["status"] == "passed", result
    detail = json.loads(result["detail"])
    assert detail["module_execution"]["files"]["domain.mjs"]["function_called"]


@pytest.mark.parametrize("uses_domain", [False, True])
def test_adapter_enforces_execution_evidence_even_when_business_report_passes(tmp_path, monkeypatch, uses_domain):
    _, plan, data, cfg = setup_task(tmp_path)
    data["modules"][0]["owned_files"] = ["main.mjs"]
    data["modules"][1]["owned_files"] = ["domain.mjs"]
    plan["checks"] = [{"id": path, "kind": "file_exists", "path": path, "requirements": ["R2"]}
                      for path in ["main.mjs", "domain.mjs"]] + [
        {"id": "business", "kind": "external", "name": "task-acceptance", "requirements": ["R1"],
         "read_paths": ["main.mjs", "domain.mjs"], "repair_paths": ["main.mjs"]}]
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    main = "import {compute} from './domain.mjs';console.log(compute())" if uses_domain else "console.log(6)"
    client = FakeClient([step({"main.mjs": main}), step({"domain.mjs": "export function compute(){return 6}"})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(replace(cfg, acceptance="node-check", acceptance_script=verifier(tmp_path), contract_repair_limit=0))
    assert outcome.ok is uses_domain
    assert outcome.reports["a"].passed and outcome.reports["b"].passed
    assert outcome.reports["__contract_composition__"].passed is uses_domain
    report = json.loads((cfg.workdir / "official-acceptance-1.json").read_text(encoding="utf-8"))
    assert report["summary"]["passed"] == 1
    evidence = json.loads((cfg.workdir / "module-execution-1.json").read_text(encoding="utf-8"))
    assert evidence["status"] == ("passed" if uses_domain else "failed")
