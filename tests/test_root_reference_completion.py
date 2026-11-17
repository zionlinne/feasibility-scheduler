import copy
import json
from dataclasses import replace

import pytest

from feasibility_scheduler.adapters.acceptance_profile import ROOT, complete_root_references
from feasibility_scheduler.adapters.contract_plan import generate_plan, validate_plan
from feasibility_scheduler.fake_client import FakeClient, Step
from test_contract_runtime import setup_task


def fixture(tmp_path):
    tasks, plan, _, _ = setup_task(tmp_path)
    profile = {"id": "evidence-index", "categories": [{"id": "requirements", "description": "Source coverage",
               "applicable": True, "allowed_kinds": ["file_exists", "json_match", "external"], "checker_names": ["suite"]}]}
    plan["acceptance_profile"] = profile
    plan["acceptance_assignments"] = [{"id": "root", "category": "requirements", "requirements": ["R1", "R2"],
                                       "owners": [ROOT], "scope": "root", "checks": ["same-total"]}]
    tasks = [replace(t, payload={**t.payload, "acceptance_profile": copy.deepcopy(profile),
                                "external_checker_names": ["suite"]}) for t in tasks]
    return tasks, plan


def test_complete_existing_root_links_without_changing_requirements_or_checks(tmp_path):
    tasks, plan = fixture(tmp_path)
    original = copy.deepcopy(plan)
    with pytest.raises(ValueError, match="missing .*R2"):
        validate_plan(plan, tasks)
    repairs = complete_root_references(plan, tasks)
    validate_plan(plan, tasks)
    assert repairs[0]["missing_requirements"] == ["R2"]
    assert repairs[0]["added_checks"] == ["a-exists", "b-exists"]
    assert plan["checks"] == original["checks"]
    assert plan["requirements"] == original["requirements"]
    expected = copy.deepcopy(original["acceptance_assignments"])
    expected[0]["checks"] += ["a-exists", "b-exists"]
    assert plan["acceptance_assignments"] == expected
    assert complete_root_references(plan, tasks) == []


def test_generated_candidate_and_completion_audit_are_separate(tmp_path):
    tasks, plan = fixture(tmp_path)
    raw = copy.deepcopy(plan)
    client = FakeClient([Step.success(body=json.dumps(raw))])
    compiled = generate_plan(client, tasks, "Compare totals using the same period.\nDeliver both offers.", audit_dir=tmp_path)
    assert json.loads((tmp_path / "contract-candidate.json").read_text()) == raw
    audit = json.loads((tmp_path / "contract-validation.json").read_text())
    assert audit["status"] == "passed"
    assert audit["completed_root_references"][0]["added_checks"] == ["a-exists", "b-exists"]
    assert compiled["checks"] == raw["checks"]


def test_business_category_cannot_gain_file_only_evidence(tmp_path):
    tasks, plan = fixture(tmp_path)
    plan["acceptance_profile"]["categories"][0]["allowed_kinds"] = ["external"]
    plan["checks"][-1] = {"id": "suite", "kind": "external", "name": "suite", "paths": ["a.json", "b.json"],
                           "requirements": ["R1"]}
    plan["acceptance_assignments"][0]["checks"] = ["suite"]
    tasks = [replace(t, payload={**t.payload, "acceptance_profile": plan["acceptance_profile"]}) for t in tasks]
    before = copy.deepcopy(plan)
    assert complete_root_references(plan, tasks) == []
    assert plan == before
    with pytest.raises(ValueError, match="checks do not cover.*R2"):
        validate_plan(plan, tasks)


def test_completion_is_atomic_when_one_requirement_has_no_approved_evidence(tmp_path):
    tasks, plan = fixture(tmp_path)
    # Source requirement R1 has a json_match check, but this category forbids it.
    plan["acceptance_profile"]["categories"][0]["allowed_kinds"] = ["external", "file_exists"]
    plan["checks"].append({"id": "suite", "kind": "external", "name": "suite", "paths": ["a.json"],
                           "requirements": ["R2"]})
    plan["acceptance_assignments"][0]["checks"] = ["suite"]
    before = copy.deepcopy(plan)
    assert complete_root_references(plan, tasks) == []
    assert plan == before


def test_local_missing_links_are_not_reassigned_to_other_tasks(tmp_path):
    tasks, plan = fixture(tmp_path)
    plan["acceptance_assignments"][0].update(scope="local", owners=["a"], checks=["a-exists"])
    before = copy.deepcopy(plan)
    assert complete_root_references(plan, tasks) == []
    assert plan == before
    with pytest.raises(ValueError, match="checks do not cover.*R1"):
        validate_plan(plan, tasks)


def test_uncovered_source_is_still_blocked_not_invented(tmp_path):
    tasks, plan = fixture(tmp_path)
    plan["checks"] = [plan["checks"][-1]]  # No predicate claims to verify R2.
    before = copy.deepcopy(plan)
    with pytest.raises(ValueError, match="uncovered source requirements"):
        complete_root_references(plan, tasks)
    assert plan == before
