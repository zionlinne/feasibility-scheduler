"""Tests for the optional open-research Skill acceptance adapter.

The reviewer is a stub; these tests verify routing of review *records*, not
independence or factual correctness of a real external reviewer.
"""

from __future__ import annotations

import pytest

from feasibility_scheduler.acceptance import run_acceptance
from feasibility_scheduler.fake_client import ApiRequest, FakeClient, Step
from feasibility_scheduler.models import Task
from feasibility_scheduler.skill_acceptance import ReviewSkill, make_skill_acceptor


SKILL = ReviewSkill(
    name="research-review-v1",
    instructions="Review the claim and source evidence.",
    criteria=("scope_alignment", "source_traceability"),
)


def delivered(task_id="report", body='{"report": "A source-backed analysis"}'):
    return FakeClient([Step.success(body)]).call(
        ApiRequest(task_id=task_id, idempotency_key="test-key", prompt="write report")
    )


def review(statuses=("PASS", "PASS")):
    return {
        "reviewer_id": "independent-reviewer-1",
        "criteria": {
            key: {
                "status": status,
                "rationale": f"Finding for {key}",
                "evidence": [f"report:{key}"] if status == "PASS" else [],
            }
            for key, status in zip(SKILL.criteria, statuses)
        },
    }


def task():
    return Task(id="report", output_contract={"type": "object"}, payload={"research": True})


def test_complete_independent_review_can_pass():
    calls = []

    def reviewer(t, response, rubric):
        calls.append((t.id, response.task_id, rubric.name))
        return review()

    report = run_acceptance(make_skill_acceptor(skill=SKILL, reviewer=reviewer), task(), delivered())
    assert report is not None and report.passed
    assert len(report.checks) == 3
    assert calls == [("report", "report", "research-review-v1")]


def test_failure_dominates_unknown():
    report = make_skill_acceptor(skill=SKILL, reviewer=lambda *_: review(("FAIL", "UNKNOWN")))(
        task(), delivered()
    )
    assert not report.passed
    assert report.error is None
    assert "scope_alignment" in report.errors[0]


def test_unknown_is_unverified_not_passed():
    report = make_skill_acceptor(skill=SKILL, reviewer=lambda *_: review(("PASS", "UNKNOWN")))(
        task(), delivered()
    )
    assert not report.passed
    assert report.error and "UNVERIFIED" in report.error


@pytest.mark.parametrize("record", [
    {},
    {"reviewer_id": "reviewer", "criteria": {}},
    {"reviewer_id": "", "criteria": review()["criteria"]},
    {"reviewer_id": "reviewer", "criteria": {
        "scope_alignment": {"status": "PASS", "rationale": "ok", "evidence": []},
        "source_traceability": {"status": "PASS", "rationale": "ok", "evidence": ["source:1"]},
    }},
    {"reviewer_id": "reviewer", "criteria": {
        "scope_alignment": {"status": "PASS", "rationale": "ok", "evidence": ["source:1"]},
        "source_traceability": {"status": "MAYBE", "rationale": "unclear", "evidence": []},
    }},
])
def test_invalid_or_incomplete_review_fails_closed(record):
    report = run_acceptance(
        make_skill_acceptor(skill=SKILL, reviewer=lambda *_: record),
        task(), delivered(),
    )
    assert report is not None and not report.passed and report.error


def test_reviewer_exception_fails_closed():
    def reviewer(*_):
        raise RuntimeError("review tool unavailable")

    report = run_acceptance(
        make_skill_acceptor(skill=SKILL, reviewer=reviewer),
        task(), delivered(),
    )
    assert report is not None and not report.passed and "RuntimeError" in report.error


def test_worker_output_cannot_serve_as_review():
    """The adapter calls the trusted callback, not a 'passed' field from Worker JSON."""
    report = make_skill_acceptor(skill=SKILL, reviewer=lambda *_: review(("FAIL", "FAIL")))(
        task(), delivered(body='{"report": "unfinished", "reviewer_id":"fake", "passed":true}')
    )
    assert not report.passed


def test_wrong_task_id_is_rejected_before_review():
    reviewer_calls = []

    def reviewer(*_):
        reviewer_calls.append(True)
        return review()

    report = make_skill_acceptor(skill=SKILL, reviewer=reviewer)(
        task(), delivered(task_id="different")
    )
    assert not report.passed
    assert not reviewer_calls


def test_skill_validation():
    with pytest.raises(ValueError):
        ReviewSkill("r", "", ("a",))
    with pytest.raises(ValueError):
        ReviewSkill("r", "rubric", ("a", "a"))


def test_executor_can_use_skill_acceptor(tmp_path):
    from feasibility_scheduler.events import EventLog
    from feasibility_scheduler.executor import Executor
    from feasibility_scheduler.statemachine import TaskState
    from feasibility_scheduler.store import TaskStore

    store = TaskStore()
    try:
        log = EventLog(tmp_path / "events.jsonl", run_id="skill-e2e")
        response = '{"report": "A short research report"}'
        acceptor = make_skill_acceptor(skill=SKILL, reviewer=lambda *_: review())
        outcome = Executor(
            [Task(id="report", constraints_declared=True,
                  output_contract={"type": "object"}, payload={"research": True})],
            store=store, log=log,
            client=FakeClient([Step.success(response)]),
            acceptor=acceptor,
        ).run()
        assert outcome.ok
        assert outcome.states["report"] is TaskState.ACCEPTED
        assert outcome.reports["report"].passed
    finally:
        store.close()
