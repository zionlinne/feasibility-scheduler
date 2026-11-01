"""executor.py：重试、派发核对、租约冲突与重启后的验收报告恢复。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from feasibility_scheduler.acceptance import check_trace
from feasibility_scheduler.events import EventLog
from feasibility_scheduler.executor import (
    ActiveLeaseError,
    DispatchInvariantError,
    Executor,
)
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.models import Constraint, Task
from feasibility_scheduler.phi import ConstraintCheck
from feasibility_scheduler.statemachine import TaskState
from feasibility_scheduler.store import TaskStore

RULE = "rule"


class SimulatedCrash(BaseException):
    pass


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def task(task_id, deps=()):
    return Task(
        id=task_id,
        dependencies=tuple(deps),
        constraints=(Constraint(RULE, True),),
        constraints_declared=True,
        output_contract={"type": "object", "required": ["ok"]},
        payload={"work": task_id},
    )


CHAIN = [task("a"), task("b", ("a",))]
CHECKS = {t: [ConstraintCheck(RULE, "SATISFIED")] for t in ("a", "b", "c")}


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    stores = []

    def make(tasks, client, **kwargs):
        store = TaskStore(tmp_path / "s.sqlite3", clock=clock)
        stores.append(store)
        log = EventLog(tmp_path / "e.jsonl", run_id="r", clock=clock)
        return Executor(tasks, store=store, log=log, client=client, checks=CHECKS, **kwargs)

    make.clock = clock
    yield make
    for store in stores:
        assert check_trace(store, EventLog(tmp_path / "e.jsonl", run_id="r").events()) == ()
        store.close()


def ok(n):
    return [Step.success('{"ok": true}')] * n


@pytest.mark.parametrize("failure", [Step.rate_limited(), Step.server_error()])
def test_retryable_failure_is_retried_with_new_key(env, failure):
    client = FakeClient([failure, *ok(2)])
    outcome = env(CHAIN, client).run()

    assert outcome.exit_code == 0
    keys = [r.idempotency_key for r in client.responses if r.task_id == "a"]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_retries_exhausted_fails_task(env):
    client = FakeClient([Step.server_error()] * 2 + ok(1))
    outcome = env(CHAIN, client, max_attempts=2).run()

    assert outcome.states["a"] is TaskState.FAILED
    assert outcome.states["b"] is TaskState.PENDING
    assert client.script_remaining == 1  # b 没有被派发
    assert outcome.exit_code != 0


def test_invalid_json_is_not_retried(env):
    client = FakeClient([Step.invalid_json(), *ok(2)])
    outcome = env(CHAIN, client).run()

    assert outcome.states["a"] is TaskState.FAILED
    assert len(client.responses) == 1
    assert outcome.exit_code != 0


def test_dispatch_guard_catches_premature_release(env, monkeypatch):
    # 即使释放判断出错，派发前的二次核对也会拦下未 ACCEPTED 上游的任务
    # 并发 2：被错误放行的 b 会和 a 在同一轮进入派发
    ex = env(CHAIN, FakeClient(ok(2)), max_concurrency=2)
    monkeypatch.setattr(ex, "_upstream_accepted", lambda task_id: True)
    with pytest.raises(DispatchInvariantError, match="尚未 ACCEPTED"):
        ex.run()


def test_active_lease_is_not_taken_over(env):
    async def crash(request):
        raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        env(CHAIN, FakeClient(ok(2)), after_send=crash).run()
    # 时钟未推进，原租约仍有效：新进程不得接管
    with pytest.raises(ActiveLeaseError):
        env(CHAIN, FakeClient(ok(2))).run()


def test_reports_are_reloaded_after_restart(env):
    client = FakeClient(ok(2))

    async def crash_on_b(request):
        if request.task_id == "b":
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        env(CHAIN, client, before_send=crash_on_b).run()
    env.clock.now += timedelta(seconds=120)
    outcome = env(CHAIN, client).run()

    # a 在上一个进程已验收；它的报告从事件日志恢复，判定不会报“缺少验收报告”
    assert outcome.exit_code == 0, outcome.verdict.reasons
    assert outcome.reports["a"].passed
    assert [r.task_id for r in client.responses] == ["a", "b"]


def test_concurrency_never_exceeds_limit(env):
    tasks = [task("a"), task("b"), task("c")]
    outcome = env(tasks, FakeClient(ok(3)), max_concurrency=2).run()
    assert outcome.exit_code == 0
    assert outcome.max_in_flight == 2


@pytest.mark.parametrize("kwargs", [{"max_concurrency": 0}, {"max_attempts": 0}])
def test_invalid_limits_are_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        Executor(CHAIN, store=TaskStore(), log=EventLog(tmp_path / "e.jsonl", run_id="r"),
                 client=FakeClient(), **kwargs)


def test_empty_graph_fails(env):
    outcome = env([], FakeClient()).run()
    assert outcome.exit_code != 0
