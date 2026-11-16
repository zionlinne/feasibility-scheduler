"""Program-level checks with scripted reviews, not model-quality evaluations."""

import json
from dataclasses import replace

import pytest

from feasibility_scheduler.adapters.exploration_acceptance import make_exploration_acceptor
from feasibility_scheduler.client import ApiRequest
from feasibility_scheduler.events import EventLog
from feasibility_scheduler.executor import Executor
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.models import Constraint, Task
from feasibility_scheduler.phi import ConstraintCheck
from feasibility_scheduler.statemachine import TaskState
from feasibility_scheduler.store import TaskStore


def task(tid="explore", deps=()):
    return Task(tid, dependencies=deps, constraints_declared=True,
                output_contract={"type": "object", "required": ["report"],
                                 "properties": {"report": {"type": "string"}}},
                payload={"question": "What can the available evidence establish?"})


def response(body=None):
    return FakeClient([Step.success(json.dumps(body or {"report": "The answer remains uncertain."}))]).call(
        ApiRequest(task_id="explore", idempotency_key="attempt-1", prompt="explore"))


def root_review(request, status="pass"):
    p = request.to_dict()
    return {"task_id": p["task_id"], "packet_sha256": request.sha256,
            "criteria": {cid: {"status": status,
                               "reason": "The report identifies evidence limits and a next step."}
                         for cid in p["criteria"]}}


def make(root=root_review, **kwargs):
    return make_exploration_acceptor(
        criteria_by_task={"explore": {"scope": "Answer the question within the available evidence.",
                                      "limits": "State unresolved alternatives and next steps."},
                          "synthesis": {"scope": "Synthesize accepted evidence without overclaiming."}},
        root_review=root, **kwargs)


@pytest.mark.parametrize("status, state", [("pass", TaskState.ACCEPTED),
                                          ("revise", TaskState.FAILED),
                                          ("unknown", TaskState.RECOVERY_REQUIRED)])
def test_real_executor_uses_root_review_and_retains_dependency_barrier(tmp_path, status, state):
    seen = []

    def root(request):
        seen.append(request.to_dict()["task_id"])
        return root_review(request, status)

    with TaskStore() as store:
        outcome = Executor([task(), task("synthesis", ("explore",))], store=store,
                           log=EventLog(tmp_path / "events.jsonl", run_id="e"),
                           client=FakeClient([Step.success('{"report":"Uncertain, but useful findings."}')] * 2),
                           acceptor=make(root), max_concurrency=2).run()
    assert outcome.states["explore"] is state
    assert outcome.verdict.trace_complete
    assert outcome.ok is (status == "pass")
    assert seen == (["explore", "synthesis"] if status == "pass" else ["explore"])
    if status != "pass":
        assert outcome.states["synthesis"] is TaskState.PENDING


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(packet_sha256="stale"),
    lambda r: r.update(task_id="other"),
    lambda r: r.update(passed=True),
    lambda r: r["criteria"].pop("scope"),
    lambda r: r["criteria"].update(extra={"status": "pass", "reason": "irrelevant"}),
    lambda r: r["criteria"]["scope"].update(status=True),
    lambda r: r["criteria"]["scope"].update(reason=""),
    lambda r: r["criteria"]["scope"].update(unexpected="field"),
])
def test_invalid_review_never_accepts(mutate):
    def root(request):
        result = root_review(request)
        mutate(result)
        return result

    report = make(root)(task(), response())
    assert not report.passed and report.error


@pytest.mark.parametrize("result", [None, True, [], {"passed": True}])
def test_missing_or_nonreview_result_cannot_pass(result):
    report = make(lambda p: result)(task(), response())
    assert not report.passed and report.error


@pytest.mark.parametrize("change", ["report", "question", "sources", "criteria", "attempt"])
def test_stale_review_is_invalidated(change):
    saved = []

    def root(packet):
        saved.append(root_review(packet))
        return saved[-1]

    assert make(root)(task(), response()).passed
    t, r = task(), response()
    if change == "report":
        r = response({"report": "Different conclusion."})
    elif change == "question":
        t = replace(t, payload={"question": "Different question."})
    elif change == "sources":
        t = replace(t, payload={**t.payload, "sources": {"s": "New evidence"}})
    elif change == "attempt":
        r = replace(r, idempotency_key="attempt-2")
    accept = make(lambda p: saved[0])
    if change == "criteria":
        accept = make_exploration_acceptor(criteria_by_task={"explore": {"scope": "Changed standard"}},
                                           root_review=lambda p: saved[0])
    report = accept(t, r)
    assert not report.passed and "does not match" in report.error


def test_worker_self_review_has_no_authority():
    report = make(lambda p: None)(task(), response({"report": "A claim.", "passed": True}))
    assert not report.passed and report.error


def test_invalid_artifact_does_not_reach_root():
    def root(packet):
        pytest.fail("schema failure must not reach qualitative review")

    report = make(root)(task(), response({"report": 123}))
    assert not report.passed and not report.error


def test_missing_task_rubric_is_not_silently_downgraded():
    report = make()(task("unmapped"), replace(response(), task_id="unmapped"))
    assert not report.passed and report.error


def test_unknown_phi_is_still_not_dispatched(tmp_path):
    t = replace(task(), constraints=(Constraint("missing-source", True),))
    with TaskStore() as store:
        client = FakeClient([Step.success()])
        outcome = Executor([t], store=store,
                           log=EventLog(tmp_path / "events.jsonl", run_id="blocked"),
                           client=client, acceptor=make()).run()
    assert not client.responses
    assert outcome.states[t.id] is TaskState.DEFERRED and not outcome.ok


def test_rubric_snapshot_and_root_reason_are_auditable():
    criteria = {"explore": {"limits": "Disclose uncertainty."}}
    accept = make_exploration_acceptor(criteria_by_task=criteria, root_review=root_review)
    criteria.clear()
    report = accept(task(), response())
    assert report.passed
    stored = next(c for c in report.checks if c.startswith("exploration_root_review:"))
    assert "The report identifies evidence limits" in stored


def test_callback_exception_requires_recovery():
    def root(packet):
        raise TimeoutError("Root review timeout")

    report = make(root)(task(), response())
    assert not report.passed and "TimeoutError" in report.error
