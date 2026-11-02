import json
from dataclasses import replace

import pytest

from feasibility_scheduler.adapters.contract_checks import check_repair_paths
from feasibility_scheduler.adapters.contract_plan import validate_plan
from feasibility_scheduler.adapters.legacy_doc import run_legacy_doc
from test_contract_runtime import setup_task, step
from feasibility_scheduler.fake_client import FakeClient


def external_fixture(tmp_path, *, legacy=False):
    tasks, plan, data, cfg = setup_task(tmp_path)
    reference = '{"total":1}'
    tasks = [replace(t, payload={**t.payload, "external_checker_names": ["suite"],
                                "reference_files": {"public/expected.json": reference}}) for t in tasks]
    check = {"id": "suite", "kind": "external", "name": "suite", "requirements": ["R1"]}
    if legacy:
        check["paths"] = ["a.json", "public/expected.json"]
    else:
        check.update(read_paths=["a.json", "public/expected.json"], repair_paths=["a.json"])
    plan["checks"] = [c for c in plan["checks"] if c["kind"] == "file_exists"] + [check]
    data["external_checker_names"] = ["suite"]
    data["reference_files"] = {"public/expected.json": reference}
    return tasks, plan, data, cfg, check


@pytest.mark.parametrize("legacy", [False, True])
def test_external_can_read_provided_material_without_granting_repair(tmp_path, legacy):
    tasks, plan, _, _, check = external_fixture(tmp_path, legacy=legacy)
    validate_plan(plan, tasks)
    assert check_repair_paths(check, owned={"a.json", "b.json"}) == {"a.json"}


@pytest.mark.parametrize("field,bad", [
    ("read_paths", ["private/verify.mjs"]),
    ("read_paths", ["../escape"]),
    ("read_paths", ["C:/outside.json"]),
    ("read_paths", ["a.json", "a.json"]),
    ("repair_paths", ["public/expected.json"]),
    ("repair_paths", ["not-owned.json"]),
    ("repair_paths", None),
])
def test_invalid_scopes_name_check_and_field(tmp_path, field, bad):
    tasks, plan, _, _, check = external_fixture(tmp_path)
    check[field] = bad
    with pytest.raises(ValueError, match=f"external check suite: {field}"):
        validate_plan(plan, tasks)


def test_ambiguous_new_and_legacy_fields_rejected(tmp_path):
    tasks, plan, _, _, check = external_fixture(tmp_path)
    check["paths"] = ["a.json"]
    with pytest.raises(ValueError, match="not both"):
        validate_plan(plan, tasks)


@pytest.mark.parametrize("legacy", [False, True])
def test_reference_only_external_has_no_repair_permission(tmp_path, legacy):
    tasks, plan, _, _, check = external_fixture(tmp_path, legacy=legacy)
    if legacy:
        check["paths"] = ["public/expected.json"]
    else:
        check.update(read_paths=["public/expected.json"], repair_paths=[])
    validate_plan(plan, tasks)
    assert check_repair_paths(check, owned={"a.json", "b.json"}) == set()


@pytest.mark.parametrize("legacy", [False, True])
def test_root_repair_receives_only_owned_outputs_and_keeps_reference_bytes(tmp_path, monkeypatch, legacy):
    _, plan, data, cfg, _ = external_fixture(tmp_path, legacy=legacy)
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([step({"a.json": '{"total":0}'}), step({"b.json": '{"total":1}'}),
                         step({"a.json": '{"total":1}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    def checker(workspace):
        expected = json.loads((workspace / "public/expected.json").read_text())
        actual = json.loads((workspace / "a.json").read_text())
        return {"status": "passed" if actual == expected else "failed", "detail": "Compare delivered output to provided material"}
    outcome = run_legacy_doc(replace(cfg, external_checkers={"suite": checker}))
    assert outcome.ok
    feedback = json.loads(next(cfg.workdir.glob("contract-repair-*.json")).read_text())
    assert feedback["repair_files"] == ["a.json"]
    assert set(feedback["artifacts"]) == {"a.json"}
    assert (cfg.workdir / "workspace/public/expected.json").read_bytes() == b'{"total":1}'


def test_local_external_failure_respects_explicit_repair_subset(tmp_path, monkeypatch):
    _, plan, data, cfg, check = external_fixture(tmp_path)
    # Both files belong to one work unit; repair permission is narrower than ownership.
    data["modules"] = [{"module_id": "a", "owned_files": ["a.json", "b.json"]}]
    plan["modules"] = plan["modules"][:1]
    check.update(read_paths=["a.json", "b.json"], repair_paths=["a.json"])
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([step({"a.json": '{"total":0}', "b.json": '{"total":1}'}),
                         step({"a.json": '{"total":1}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    def checker(workspace):
        equal = json.loads((workspace / "a.json").read_text()) == json.loads((workspace / "b.json").read_text())
        return {"status": "passed" if equal else "failed", "detail": "Actual totals disagree"}
    outcome = run_legacy_doc(replace(cfg, external_checkers={"suite": checker}))
    assert outcome.ok
    feedback = json.loads(next(cfg.workdir.glob("contract-repair-*.json")).read_text())
    assert feedback["task_id"] == "a" and feedback["repair_files"] == ["a.json"]
    assert (cfg.workdir / "workspace/b.json").read_bytes() == b'{"total":1}'


def test_repair_response_cannot_smuggle_readonly_file(tmp_path, monkeypatch):
    _, _, data, cfg, _ = external_fixture(tmp_path)
    cfg.task_input_path.write_text(json.dumps(data), encoding="utf-8")
    client = FakeClient([step({"a.json": '{"total":0}'}), step({"b.json": '{"total":1}'}),
                         step({"a.json": '{"total":1}', "public/expected.json": '{"total":999}'})])
    monkeypatch.setattr("feasibility_scheduler.adapters.legacy_doc.create_client", lambda *a, **k: client)
    def checker(workspace):
        return {"status": "failed", "detail": "Force bounded repair"}
    outcome = run_legacy_doc(replace(cfg, external_checkers={"suite": checker}))
    assert not outcome.ok
    assert "exactly the assigned artifacts" in outcome.reports["__contract_composition__"].errors[0]
    assert (cfg.workdir / "workspace/public/expected.json").read_bytes() == b'{"total":1}'
