"""acceptance.py：契约验收、验收结果归一与总运行判定（规格第 5 节）。"""

from __future__ import annotations

import pytest

from feasibility_scheduler.acceptance import (
    EXIT_FAILED,
    EXIT_OK,
    AcceptanceReport,
    ContractError,
    check_trace,
    contract_acceptor,
    judge_run,
    run_acceptance,
)
from feasibility_scheduler.events import EventLog
from feasibility_scheduler.fake_client import ApiRequest, FakeClient, Step
from feasibility_scheduler.models import Task
from feasibility_scheduler.statemachine import TaskState
from feasibility_scheduler.store import TaskStore

CONTRACT = {
    "type": "object",
    "required": ["ok", "count"],
    "properties": {"ok": {"type": "boolean"}, "count": {"type": "integer"}},
}


def make_task(contract=CONTRACT, task_id="t"):
    return Task(id=task_id, output_contract=contract, payload={"work": True})


def respond(step, task_id="t"):
    client = FakeClient([step])
    return client.call(ApiRequest(task_id=task_id, idempotency_key="k", prompt="p"))


# ------------------------------------------------------------ 契约验收


def test_contract_acceptor_passes_matching_body():
    report = contract_acceptor(make_task(), respond(Step.success('{"ok": true, "count": 2}')))
    assert report.passed
    assert "required:count" in report.checks


@pytest.mark.parametrize(
    "body, error",
    [
        ('{"ok": true}', "缺少必需字段 'count'"),
        ('{"ok": "yes", "count": 1}', "字段 'ok' 的类型不是 boolean"),
        ('{"ok": true, "count": true}', "字段 'count' 的类型不是 integer"),
    ],
)
def test_contract_acceptor_rejects_contract_violations(body, error):
    report = contract_acceptor(make_task(), respond(Step.success(body)))
    assert not report.passed
    assert error in report.errors


def test_contract_acceptor_rejects_non_success_response():
    report = contract_acceptor(make_task(), respond(Step.invalid_json()))
    assert not report.passed
    assert any("无法解析" in item for item in report.errors)


def test_contract_acceptor_rejects_response_of_other_task():
    report = contract_acceptor(
        make_task(), respond(Step.success('{"ok": true, "count": 1}'), task_id="other")
    )
    assert not report.passed


@pytest.mark.parametrize(
    "contract", [None, {"type": "object", "pattern": "x"}, {"type": "tuple"}]
)
def test_contract_acceptor_cannot_interpret_contract(contract):
    with pytest.raises(ContractError):
        contract_acceptor(make_task(contract), respond(Step.success('{"ok": true}')))


# ------------------------------------------------------------ 结果归一


def test_run_acceptance_turns_exception_into_error_report():
    def boom(task, response):
        raise RuntimeError("x")

    report = run_acceptance(boom, make_task(), respond(Step.success()))
    assert report is not None and not report.passed
    assert "RuntimeError" in report.error


def test_run_acceptance_keeps_missing_report_as_none():
    assert run_acceptance(lambda t, r: None, make_task(), respond(Step.success())) is None


@pytest.mark.parametrize("value", [True, {"passed": True}, AcceptanceReport("other", True)])
def test_run_acceptance_rejects_malformed_report(value):
    report = run_acceptance(lambda t, r: value, make_task(), respond(Step.success()))
    assert not report.passed and report.error


def test_report_cannot_pass_with_errors():
    with pytest.raises(ValueError):
        AcceptanceReport("t", True, errors=("bad",))
    with pytest.raises(ValueError):
        AcceptanceReport("t", True, error="crash")


def test_report_round_trips_through_dict():
    report = AcceptanceReport("t", False, ("a",), ("b",), None)
    assert AcceptanceReport.from_dict(report.to_dict()) == report


# ------------------------------------------------------------ 总运行判定


@pytest.fixture
def run_env(tmp_path):
    store = TaskStore()
    log = EventLog(tmp_path / "events.jsonl", run_id="r")
    log.record("run_created", component="test")
    yield store, log
    store.close()


def accept_task(store, log, task_id):
    """按迁移表把任务推进到 ACCEPTED，存储与日志同步记录。"""
    store.create_task(task_id)
    log.record("task_validated", component="test", task_id=task_id, new_state="PENDING")
    path = ["FEASIBILITY_CHECK", "READY", "CLAIMED", "RUNNING", "RESULT_RECEIVED",
            "ACCEPTANCE_PENDING", "ACCEPTED"]
    previous = "PENDING"
    for target in path:
        if target == "CLAIMED":
            store.claim(task_id, "w", idempotency_key=f"{task_id}-k")
        else:
            store.transition(task_id, target)
        log.record("task_ready", component="test", task_id=task_id,
                   previous_state=previous, new_state=target)
        previous = target


def test_judge_ok_only_when_everything_passes(run_env):
    store, log = run_env
    accept_task(store, log, "a")
    verdict = judge_run(task_ids=["a"], store=store, log_events=log.events(),
                        reports={"a": AcceptanceReport("a", True)})
    assert verdict.ok and verdict.exit_code == EXIT_OK
    assert verdict.trace_complete and verdict.scheduler_completed


@pytest.mark.parametrize(
    "reports, reason",
    [
        ({}, "缺少验收报告"),
        ({"a": AcceptanceReport("a", False, errors=("x",))}, "验收失败"),
        ({"a": AcceptanceReport("a", False, error="boom")}, "验收程序异常"),
    ],
)
def test_scheduler_completed_does_not_override_acceptance(run_env, reports, reason):
    store, log = run_env
    accept_task(store, log, "a")  # 存储里是 ACCEPTED，但报告不支持
    verdict = judge_run(task_ids=["a"], store=store, log_events=log.events(), reports=reports)
    assert verdict.scheduler_completed
    assert not verdict.ok and verdict.exit_code == EXIT_FAILED
    assert any(reason in item for item in verdict.reasons)


def test_judge_fails_when_task_not_accepted(run_env):
    store, log = run_env
    store.create_task("a")
    log.record("task_validated", component="test", task_id="a", new_state="PENDING")
    verdict = judge_run(task_ids=["a"], store=store, log_events=log.events(),
                        reports={"a": AcceptanceReport("a", True)})
    assert not verdict.scheduler_completed and not verdict.ok


def test_judge_fails_on_store_log_divergence(run_env):
    store, log = run_env
    accept_task(store, log, "a")
    store.create_task("ghost")  # 只写了存储，没有写日志
    verdict = judge_run(task_ids=["a"], store=store, log_events=log.events(),
                        reports={"a": AcceptanceReport("a", True)})
    assert not verdict.trace_complete and not verdict.ok
    assert any("ghost" in item for item in verdict.reasons)


def test_check_trace_requires_run_created(tmp_path):
    store = TaskStore()
    log = EventLog(tmp_path / "e.jsonl", run_id="r")
    store.create_task("a")
    log.record("task_validated", component="test", task_id="a", new_state="PENDING")
    assert any("run_created" in item for item in check_trace(store, log.events()))
    store.close()


def test_check_trace_reports_unreplayable_log(run_env):
    store, log = run_env
    log.record("task_ready", component="test", task_id="a",
               previous_state="PENDING", new_state="ACCEPTED")
    assert any("无法重放" in item for item in check_trace(store, log.events()))


@pytest.mark.parametrize("gate", [False, None])
def test_external_gate_must_be_true(run_env, gate):
    store, log = run_env
    accept_task(store, log, "a")
    verdict = judge_run(task_ids=["a"], store=store, log_events=log.events(),
                        reports={"a": AcceptanceReport("a", True)},
                        external_gates={"SpecTestsPass": gate})
    assert not verdict.ok


def test_empty_run_and_graph_errors_fail(run_env):
    store, log = run_env
    assert not judge_run(task_ids=[], store=store, log_events=log.events(), reports={}).ok
    verdict = judge_run(task_ids=["a"], store=store, log_events=log.events(), reports={},
                        graph_errors=["cycle detected"])
    assert not verdict.ok and any("cycle detected" in item for item in verdict.reasons)
