"""Adapter policy tests. Scripted reviews here are not real model evaluations."""

import json
from dataclasses import replace

import pytest

from feasibility_scheduler.acceptance import run_acceptance
from feasibility_scheduler.adapters.research_acceptance import make_research_acceptor
from feasibility_scheduler.client import ApiRequest
from feasibility_scheduler.events import EventLog
from feasibility_scheduler.executor import Executor
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.models import Constraint, Task
from feasibility_scheduler.phi import ConstraintCheck
from feasibility_scheduler.statemachine import TaskState
from feasibility_scheduler.store import TaskStore


CRITERIA = {"scope": "Answer the bounded question and state evidence limits.",
            "evidence": "Support the claim with the frozen primary source."}
SOURCES = {"source": "The scheduler releases downstream only after ACCEPTED."}
ARTIFACT = {"report": "Downstream waits for acceptance. Review quality remains uncertain."}
CONTRACT = {"type": "object", "required": ["report"],
            "properties": {"report": {"type": "string"}}}


def task(tid="research", deps=()):
    return Task(tid, dependencies=deps,
                constraints=(Constraint("bounded", True),), constraints_declared=True,
                output_contract=CONTRACT, payload={"question": "When is downstream released?"})


def response(artifact=ARTIFACT):
    return FakeClient([Step.success(json.dumps(artifact))]).call(
        ApiRequest(task_id="research", idempotency_key="worker-attempt-1", prompt="research"))


def good_review(packet, status="pass"):
    data = packet.to_dict()
    return {"task_id": data["task_id"], "packet_sha256": packet.sha256,
            "reviewer_id": data["reviewer_id"],
            "assessments": [{"criterion_id": cid, "status": status,
                             "reason": "Scoped claim supported; quality not proven.",
                             "report_quote": "Downstream waits for acceptance.",
                             "source_quotes": {"source": "only after ACCEPTED"}}
                            for cid in data["criteria"]]}


def acceptor(tmp_path, reviewer=good_review, **kwargs):
    args = dict(criteria=CRITERIA, sources=SOURCES, skill_text="Apply the frozen rubric.",
                reviewer_id="root",
                reviewer=reviewer, audit_dir=tmp_path)
    args.update(kwargs)
    return make_research_acceptor(**args)


def test_pass_archives_complete_bound_packet_and_actual_review(tmp_path):
    report = acceptor(tmp_path)(task(), response())
    assert report.passed and report.error is None
    packets = list(tmp_path.glob("packet-*.json"))
    reviews = list(tmp_path.glob("review-*.json"))
    assert len(packets) == len(reviews) == 1
    packet = json.loads(packets[0].read_text())
    review = json.loads(reviews[0].read_text())
    assert packet["artifact"] == ARTIFACT
    assert packet["task"]["constraints"][0]["name"] == "bounded"
    assert packet["task"]["payload"] == task().payload
    assert packet["sources"] == SOURCES
    assert packet["criteria"] == CRITERIA
    assert review["packet_sha256"] in packets[0].name
    assert all(f"research_criterion:{cid}:pass" in report.checks for cid in CRITERIA)


@pytest.mark.parametrize("status, state", [("pass", TaskState.ACCEPTED),
                                          ("revise", TaskState.FAILED),
                                          ("unknown", TaskState.RECOVERY_REQUIRED)])
def test_executor_preserves_dependency_barrier_and_trace(tmp_path, status, state):
    calls = []

    def review(packet):
        calls.append(packet.to_dict()["task_id"])
        return good_review(packet, status)

    tasks = [task(), task("synthesis", ("research",))]
    client = FakeClient([Step.success(json.dumps(ARTIFACT))] * 2)
    with TaskStore() as store:
        log = EventLog(tmp_path / "events.jsonl", run_id="review-test")
        outcome = Executor(tasks, store=store, log=log, client=client,
                           acceptor=acceptor(tmp_path / "audit", review), max_concurrency=2,
                           checks={t.id: [ConstraintCheck("bounded", "SATISFIED")]
                                   for t in tasks}).run()
    assert outcome.states["research"] is state
    assert outcome.verdict.trace_complete
    assert outcome.ok is (status == "pass")
    assert calls == (["research", "synthesis"] if status == "pass" else ["research"])
    if status != "pass":
        assert outcome.states["synthesis"] is TaskState.PENDING
        assert len(client.responses) == 1


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(packet_sha256="stale"),
    lambda r: r.update(task_id="other"),
    lambda r: r.update(reviewer_id="unassigned-worker"),
    lambda r: r.update(passed=True),
    lambda r: r["assessments"].pop(),
    lambda r: r["assessments"].__setitem__(1, r["assessments"][0]),
    lambda r: r["assessments"][0].update(criterion_id="invented"),
    lambda r: r["assessments"][0].update(status=True),
    lambda r: r["assessments"][0].update(reason=""),
    lambda r: r["assessments"][0].update(report_quote="fabricated passage"),
    lambda r: r["assessments"][0].update(report_quote=""),
    lambda r: r["assessments"][0].update(source_quotes={}),
    lambda r: r["assessments"][0].update(source_quotes={"invented": "quote"}),
    lambda r: r["assessments"][0].update(source_quotes={"source": "fabrication"}),
    lambda r: r["assessments"][0].update(source_quotes={"source": ""}),
])
def test_invalid_or_unbound_reviews_cannot_accept(tmp_path, mutate):
    def reviewer(packet):
        result = good_review(packet)
        mutate(result)
        return result

    report = acceptor(tmp_path, reviewer)(task(), response())
    assert not report.passed and report.error
    assert list(tmp_path.glob("review-*.json"))  # rejected review retained too


@pytest.mark.parametrize("result", [None, True, [], {"passed": True}])
def test_unusable_review_enters_recovery(tmp_path, result):
    report = acceptor(tmp_path, lambda p: result)(task(), response())
    assert not report.passed and report.error


def test_reviewer_timeout_not_retried_or_converted_to_pass(tmp_path):
    calls = []

    def reviewer(packet):
        calls.append(packet.sha256)
        raise TimeoutError("review deadline elapsed")

    report = acceptor(tmp_path, reviewer)(task(), response())
    assert len(calls) == 1
    assert not report.passed and "TimeoutError" in report.error
    assert any(c.startswith("research_packet:") for c in report.checks)


@pytest.mark.parametrize("bad_response", [response({"self_review": {"passed": True}}),
                                         replace(response(), task_id="other")])
def test_invalid_delivery_never_calls_reviewer(tmp_path, bad_response):
    def reviewer(packet):
        pytest.fail("invalid worker delivery must not reach semantic review")

    report = acceptor(tmp_path, reviewer)(task(), bad_response)
    assert not report.passed and not report.error
    assert not list(tmp_path.glob("packet-*.json"))


def test_revision_of_absent_section_needs_no_fabricated_quote(tmp_path):
    def reviewer(packet):
        r = good_review(packet, "revise")
        for item in r["assessments"]:
            item.update(reason="Required limitations section absent.", report_quote="", source_quotes={})
        return r

    report = acceptor(tmp_path, reviewer)(task(), response())
    assert not report.passed and not report.error and len(report.errors) == 2


@pytest.mark.parametrize("change", ["artifact", "question", "rubric", "sources", "skill", "attempt"])
def test_old_review_cannot_accept_changed_packet(tmp_path, change):
    saved = []

    def record(packet):
        saved.append(good_review(packet))
        return saved[-1]

    assert acceptor(tmp_path, record)(task(), response()).passed
    kwargs = {}
    t, r = task(), response()
    if change == "artifact":
        r = response({"report": ARTIFACT["report"] + " Revised."})
    elif change == "question":
        t = replace(t, payload={"question": "A different question"})
    elif change == "rubric":
        kwargs["criteria"] = {**CRITERIA, "scope": "Changed obligation."}
    elif change == "sources":
        kwargs["sources"] = {"source": SOURCES["source"] + " Updated."}
    elif change == "skill":
        kwargs["skill_text"] = "A different review procedure."
    elif change == "attempt":
        r = replace(r, idempotency_key="worker-attempt-2")
    report = acceptor(tmp_path, lambda p: saved[0], **kwargs)(t, r)
    assert not report.passed and "packet_sha256 mismatch" in report.error


def test_host_configuration_is_snapshotted(tmp_path):
    criteria, sources = dict(CRITERIA), dict(SOURCES)
    a = acceptor(tmp_path, criteria=criteria, sources=sources)
    criteria.clear()
    sources.clear()
    assert a(task(), response()).passed


@pytest.mark.parametrize("kwargs", [{"reviewer_id": ""},
                                    {"criteria": {}}, {"sources": {}},
                                    {"skill_text": ""}, {"require_sources": "false"}])
def test_invalid_trust_configuration_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        acceptor(tmp_path, **kwargs)


def test_root_can_apply_skill_without_an_additional_agent(tmp_path):
    seen = []

    def root_review(packet):
        seen.append(packet.to_dict()["reviewer_id"])
        return good_review(packet)

    assert acceptor(tmp_path, root_review)(task(), response()).passed
    assert seen == ["root"]


def test_explicit_source_free_mode_remains_possible(tmp_path):
    def reviewer(packet):
        r = good_review(packet)
        for item in r["assessments"]:
            item["source_quotes"] = {}
        return r

    assert acceptor(tmp_path, reviewer, sources={}, require_sources=False)(task(), response()).passed


def test_unwritable_audit_archive_cannot_accept(tmp_path):
    target = tmp_path / "file"
    target.write_text("not a directory")
    report = acceptor(target)(task(), response())
    assert not report.passed and report.error


def test_existing_normalizer_preserves_fail_closed_adapter_report(tmp_path):
    report = run_acceptance(acceptor(tmp_path, lambda p: None), task(), response())
    assert not report.passed and report.error
