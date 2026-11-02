import json
from dataclasses import replace

from feasibility_scheduler.adapters.legacy_doc import run_legacy_doc
from feasibility_scheduler.fake_client import FakeClient, Step
from test_contract_runtime import setup_task, step


def setup_entries(tmp_path):
    _, plan, data, cfg = setup_task(tmp_path)
    for module, path in zip(data["modules"], ["a.mjs", "b.mjs"]):
        module.update(owned_files=[path], estimated_tokens=500)
    plan["checks"] = [{"id": path, "kind": "file_exists", "path": path, "requirements": ["R1", "R2"]}
                      for path in ["a.mjs", "b.mjs"]]
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    return replace(cfg, policy="rewrite", acceptance="node-check")


def test_exhausted_merge_splits_back_and_reaches_root_without_remerging(tmp_path, monkeypatch):
    cfg = setup_entries(tmp_path)
    client = FakeClient([Step.server_error(), Step.server_error(), Step.server_error(),
                         step({"a.mjs": "export const a=1;"}), step({"b.mjs": "export const b=2;"})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(cfg)
    assert outcome.ok and outcome.verdict.trace_complete
    assert outcome.reports["__contract_composition__"].passed
    summary = json.loads((cfg.workdir / "summary.json").read_text(encoding="utf-8"))
    kinds = [r["kind"] for r in summary["rewrite_history"]]
    assert kinds.count("merge") == 1 and kinds.count("recovery_split") == 1
    assert set(summary["written_files"]) == {"a.mjs", "b.mjs"}
    events = [json.loads(x) for x in (cfg.workdir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len([e for e in events if e["type"] == "api_request_started" and e.get("task_id") == "merge_a_b"]) == 3
    applied = next(e for e in events if e["type"] == "graph_rewrite_applied" and e["details"]["kind"] == "recovery_split")
    assert "merge_a_b" in applied["details"]["removed_ids"]
    assert len(applied["details"]["added_ids"]) == 2


def test_ambiguous_timeout_does_not_trigger_new_child_calls(tmp_path, monkeypatch):
    cfg = setup_entries(tmp_path)
    client = FakeClient([Step.timeout()])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    outcome = run_legacy_doc(cfg)
    assert not outcome.ok
    assert outcome.states["merge_a_b"].value == "RECOVERY_REQUIRED"
    summary = json.loads((cfg.workdir / "summary.json").read_text(encoding="utf-8"))
    assert not any(r["kind"] == "recovery_split" for r in summary["rewrite_history"])


def test_recovered_children_cannot_be_recovered_or_merged_again(tmp_path):
    from feasibility_scheduler.adapters.graph_rewrite import generate_candidates, recover_failed_merge
    from feasibility_scheduler.adapters.legacy_doc import build_legacy_tasks
    cfg = setup_entries(tmp_path)
    data = json.loads(cfg.task_input_path.read_text())
    tasks = build_legacy_tasks(data, task_md=cfg.task_md_path.read_text())
    merged = next(c for c in generate_candidates(tasks, states={}) if c.kind == "merge")
    task = merged.tasks[0]
    recovered = recover_failed_merge(merged.tasks, task)
    assert recovered is not None
    assert all(t.payload["disable_merge"] for t in recovered.tasks)
    assert all(recover_failed_merge(recovered.tasks, t) is None for t in recovered.tasks)
    assert not any(c.kind == "merge" for c in generate_candidates(recovered.tasks, states={}))
