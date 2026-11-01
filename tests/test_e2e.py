"""端到端测试：五张固定任务图 + 三种恢复场景 + 验收失败门禁（规格第 4、5、6 节）。

每个测试通过 ``harness`` 夹具创建状态存储与事件日志；夹具在测试结束时
**独立地**用 :func:`replay_file` 重放 JSONL，逐任务比较重放状态、迁移序列与
状态存储，不依赖执行器自己的一致性判断。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from feasibility_scheduler import executor as executor_module
from feasibility_scheduler.acceptance import AcceptanceReport
from feasibility_scheduler.dag import build_dag
from feasibility_scheduler.events import EventLog, read_events
from feasibility_scheduler.executor import Executor
from feasibility_scheduler.fake_client import FakeClient, RequestStatus, Step
from feasibility_scheduler.models import Constraint, Task
from feasibility_scheduler.phi import ConstraintCheck
from feasibility_scheduler.replay import replay_file
from feasibility_scheduler.statemachine import TaskState
from feasibility_scheduler.store import TaskStore
from feasibility_scheduler.timing import analyze

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
CONTRACT = {"type": "object", "required": ["ok"]}
CHECKED = "contract_declared"


class SimulatedCrash(BaseException):
    """模拟进程被杀：不是 Exception，不会被任何业务层 except 吞掉。"""


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


LEASE_SECONDS = 60


@dataclass
class Harness:
    """一次运行的持久化环境；``executor()`` 每次调用都模拟一个新进程。"""

    tmp: Path
    clock: MutableClock = field(default_factory=MutableClock)
    run_id: str = "e2e-run"
    stores: list[TaskStore] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.tmp / "state.sqlite3"

    @property
    def log_path(self) -> Path:
        return self.tmp / "events.jsonl"

    def executor(self, tasks, client, **kwargs) -> Executor:
        store = TaskStore(self.db_path, clock=self.clock)
        log = EventLog(self.log_path, run_id=self.run_id, clock=self.clock)
        self.stores.append(store)
        kwargs.setdefault("lease_seconds", LEASE_SECONDS)
        return Executor(tasks, store=store, log=log, client=client, **kwargs)

    def assert_replay_matches_store(self):
        """用事件日志重放，逐任务比较最终状态与迁移序列。"""
        replayed = replay_file(self.log_path)
        store = TaskStore(self.db_path, clock=self.clock)
        try:
            stored = {record.task_id: record.state for record in store.list_tasks()}
            assert {key: item.state for key, item in replayed.tasks.items()} == stored
            for task_id, item in replayed.tasks.items():
                expected = tuple(
                    (event.from_state, event.to_state) for event in store.events(task_id)
                )
                assert item.transitions == expected, task_id
        finally:
            store.close()
        return replayed

    def close(self) -> None:
        for store in self.stores:
            store.close()


@pytest.fixture
def harness(tmp_path):
    env = Harness(tmp_path)
    yield env
    env.close()
    # 每个端到端测试结束后都核对一次：日志重放状态必须等于状态存储
    if env.log_path.exists():
        env.assert_replay_matches_store()


def declared(tasks):
    """显式声明一条可验证约束并给出满足证据，使 Φ=TRUE。

    固定图文件省略约束字段（Φ=UNKNOWN）；这里不靠空约束列表放行，而是
    声明约束并配上确定性证据。
    """
    result = [
        Task(
            id=task.id,
            dependencies=task.dependencies,
            constraints=(Constraint(CHECKED, True),),
            constraints_declared=True,
            output_contract=CONTRACT,
            payload=task.payload,
        )
        for task in tasks
    ]
    checks = {task.id: [ConstraintCheck(CHECKED, "SATISFIED")] for task in result}
    return result, checks


def ok_steps(count):
    return [Step.success('{"ok": true}') for _ in range(count)]


def events_of(harness, event_type, task_id=None):
    return [
        event
        for event in replay_events(harness)
        if event.type == event_type and (task_id is None or event.task_id == task_id)
    ]


def replay_events(harness):
    return read_events(harness.log_path)


# ===================================================== 上游未 ACCEPTED 不释放下游


@pytest.mark.parametrize(
    "hold_state", [TaskState.RESULT_RECEIVED, TaskState.ACCEPTANCE_PENDING]
)
def test_downstream_not_dispatched_while_upstream_awaits_acceptance(
    harness, load_graph, hold_state
):
    """菱形图：L 已 ACCEPTED，R 停在 hold_state；其间调度循环确实又做了释放检查，
    J 仍为 PENDING、未派发、未发请求。"""
    tasks, checks = declared(load_graph("02_diamond.json").tasks)
    client = FakeClient(ok_steps(4))
    observed = {}
    marks = {}
    holder = {}

    async def on_transition(task_id, previous, target):
        ex = holder["executor"]
        if task_id == "L" and target is TaskState.ACCEPTED:
            marks["passes_at_L"] = ex.release_passes
        if task_id == "R" and target is hold_state:
            for _ in range(1000):
                if "passes_at_L" in marks and ex.release_passes > marks["passes_at_L"]:
                    break
                await asyncio.sleep(0)
            observed["L"] = ex.store.get_task("L").state
            observed["R"] = ex.store.get_task("R").state
            observed["J"] = ex.store.get_task("J").state
            observed["J_dispatched"] = any(t == "J" for t, _ in ex.dispatch_log)
            observed["J_requests"] = [r for r in client.responses if r.task_id == "J"]
            observed["release_passes_after_L"] = (
                ex.release_passes - marks.get("passes_at_L", ex.release_passes)
            )

    ex = harness.executor(
        tasks, client, checks=checks, max_concurrency=2, on_transition=on_transition
    )
    holder["executor"] = ex
    outcome = ex.run()

    assert observed["L"] is TaskState.ACCEPTED
    assert observed["R"] is hold_state
    assert observed["release_passes_after_L"] >= 1  # L 完成后确实又检查过释放
    assert observed["J"] is TaskState.PENDING
    assert observed["J_dispatched"] is False
    assert observed["J_requests"] == []

    # 放行后 J 才派发；派发时两个上游都已 ACCEPTED
    assert outcome.exit_code == 0
    j_dispatch = [upstream for t, upstream in ex.dispatch_log if t == "J"]
    assert j_dispatch == [{"L": TaskState.ACCEPTED, "R": TaskState.ACCEPTED}]
    claimed_j = events_of(harness, "task_claimed", "J")[0].sequence
    for upstream in ("L", "R"):
        assert events_of(harness, "acceptance_passed", upstream)[0].sequence < claimed_j


def test_linear_graph_runs_in_dependency_order(harness, load_graph):
    tasks, checks = declared(load_graph("01_linear.json").tasks)
    client = FakeClient(ok_steps(2))
    outcome = harness.executor(tasks, client, checks=checks).run()

    assert outcome.exit_code == 0
    assert outcome.verdict.ok
    assert dict(outcome.states) == {"collect": TaskState.ACCEPTED, "report": TaskState.ACCEPTED}
    assert [r.task_id for r in client.responses] == ["collect", "report"]
    accepted_collect = events_of(harness, "acceptance_passed", "collect")[0].sequence
    assert accepted_collect < events_of(harness, "task_claimed", "report")[0].sequence
    assert events_of(harness, "run_completed")


def test_explicit_empty_constraints_run_through_the_whole_pipeline(harness):
    """显式空约束（constraints_declared=True 且集合为空）不被输入校验拦下，
    经 Φ=TRUE、门禁 READY 一路走到执行器完成、独立验收通过。"""
    tasks = [
        Task(
            id="collect",
            constraints=(),
            constraints_declared=True,
            output_contract=CONTRACT,
            payload={"work": True},
        ),
        Task(
            id="report",
            dependencies=("collect",),
            constraints=(),
            constraints_declared=True,
            output_contract=CONTRACT,
            payload={"work": True},
        ),
    ]
    client = FakeClient(ok_steps(2))
    outcome = harness.executor(tasks, client).run()

    assert outcome.exit_code == 0
    assert dict(outcome.states) == {
        "collect": TaskState.ACCEPTED,
        "report": TaskState.ACCEPTED,
    }
    assert [r.task_id for r in client.responses] == ["collect", "report"]
    assert [
        (event.details["phi"], event.details["decision"])
        for event in events_of(harness, "phi_evaluated")
    ] == [("TRUE", "READY"), ("TRUE", "READY")]
    assert [event.task_id for event in events_of(harness, "task_ready")] == [
        "collect",
        "report",
    ]
    assert events_of(harness, "task_validated")  # 校验放行后才登记，未被拒绝
    assert events_of(harness, "run_completed")


def test_linear_graph_without_declared_constraints_is_deferred(harness, load_graph):
    # 固定图省略约束字段 → Φ=UNKNOWN → DEFERRED，不默认放行
    client = FakeClient(ok_steps(2))
    outcome = harness.executor(load_graph("01_linear.json").tasks, client).run()

    assert outcome.exit_code != 0
    assert outcome.states["collect"] is TaskState.DEFERRED
    assert outcome.states["report"] is TaskState.PENDING
    assert client.responses == ()
    assert events_of(harness, "run_failed")


@pytest.mark.parametrize("max_concurrency, expected_peak", [(1, 1), (2, 2), (4, 2)])
def test_diamond_graph_respects_concurrency_and_timing(
    harness, load_graph, max_concurrency, expected_peak
):
    graph = load_graph("02_diamond.json")
    tasks, checks = declared(graph.tasks)
    timing = analyze(build_dag(tasks), graph.durations, graph.deadlines)
    client = FakeClient(ok_steps(4))
    ex = harness.executor(
        tasks, client, checks=checks, timing=timing, max_concurrency=max_concurrency
    )
    outcome = ex.run()

    assert outcome.exit_code == 0
    assert all(state is TaskState.ACCEPTED for state in outcome.states.values())
    assert outcome.max_in_flight == expected_peak  # L、R 只有在并发≥2 时同时执行
    assert [task_id for task_id, _ in ex.dispatch_log] == ["S", "L", "R", "J"]
    for task_id, upstream in ex.dispatch_log:
        assert all(state is TaskState.ACCEPTED for state in upstream.values()), task_id


def test_unknown_graph_dispatches_only_verified_task(harness, load_graph):
    graph = load_graph("03_unknown.json")
    timing = analyze(build_dag(graph.tasks), graph.durations, graph.deadlines)
    client = FakeClient(ok_steps(3))
    outcome = harness.executor(
        graph.tasks, client, checks=graph.checks, timing=timing
    ).run()

    # fetch 有满足证据 → 执行并验收；parse 约束字段缺省 → DEFERRED；store 不释放
    assert outcome.states["fetch"] is TaskState.ACCEPTED
    assert outcome.states["parse"] is TaskState.DEFERRED
    assert outcome.states["store"] is TaskState.PENDING
    assert [r.task_id for r in client.responses] == ["fetch"]
    assert outcome.exit_code != 0
    assert not outcome.verdict.scheduler_completed


@pytest.mark.parametrize("declare", [False, True])
def test_deadline_miss_graph_is_blocked_and_never_dispatched(
    harness, load_graph, declare
):
    graph = load_graph("04_deadline_miss.json")
    tasks, checks = declared(graph.tasks) if declare else (graph.tasks, {})
    timing = analyze(build_dag(tasks), graph.durations, graph.deadlines)
    client = FakeClient(ok_steps(3))
    outcome = harness.executor(tasks, client, checks=checks, timing=timing).run()

    # 即使约束全部满足，LS<ES 的时间违反也让 Φ=FALSE → BLOCKED
    assert outcome.states["design"] is TaskState.BLOCKED
    assert outcome.states["build"] is TaskState.PENDING
    assert outcome.states["ship"] is TaskState.PENDING
    assert client.responses == ()
    assert outcome.exit_code != 0
    blocked = events_of(harness, "task_blocked", "design")
    assert blocked and "latest_start" in blocked[0].decision_reason


def test_cycle_graph_is_rejected_before_execution(harness, load_graph):
    client = FakeClient(ok_steps(2))
    outcome = harness.executor(load_graph("05_cycle.json").tasks, client).run()

    assert outcome.exit_code != 0
    assert any("cycle detected" in reason for reason in outcome.verdict.reasons)
    assert client.responses == ()
    assert dict(outcome.states) == {}
    assert [event.type for event in replay_events(harness)] == ["run_created", "run_failed"]


# ===================================================== 恢复场景


def crash_once(task_id):
    fired = []

    async def hook(request):
        if request.task_id == task_id and not fired:
            fired.append(request.idempotency_key)
            raise SimulatedCrash(request.idempotency_key)

    return hook, fired


def crash_first_run(harness, tasks, checks, client, **hooks):
    with pytest.raises(SimulatedCrash):
        harness.executor(tasks, client, checks=checks, **hooks).run()


def test_recovery_sent_reuses_result_without_resending(harness, load_graph):
    tasks, checks = declared(load_graph("01_linear.json").tasks)
    client = FakeClient(ok_steps(2))  # collect 一次 + report 一次，没有多余脚本
    hook, fired = crash_once("collect")
    crash_first_run(harness, tasks, checks, client, after_send=hook)

    first_key = fired[0]
    assert client.request_status(first_key) is RequestStatus.SENT
    harness.clock.advance(LEASE_SECONDS + 1)  # 原执行槽的租约过期

    outcome = harness.executor(tasks, client, checks=checks).run()

    assert outcome.exit_code == 0
    assert outcome.states["collect"] is TaskState.ACCEPTED
    assert client.attempts(first_key) == 1
    assert [r.task_id for r in client.responses] == ["collect", "report"]
    recovered = [
        e for e in events_of(harness, "api_response_received", "collect")
        if e.details.get("recovered")
    ]
    assert len(recovered) == 1
    assert recovered[0].details["idempotency_key"] == first_key
    assert events_of(harness, "acceptance_passed", "collect")


def test_recovery_not_sent_redispatches_with_new_key(harness, load_graph):
    tasks, checks = declared(load_graph("01_linear.json").tasks)
    client = FakeClient(ok_steps(2))
    hook, fired = crash_once("collect")
    crash_first_run(harness, tasks, checks, client, before_send=hook)

    first_key = fired[0]
    assert client.request_status(first_key) is RequestStatus.NOT_SENT
    assert client.responses == ()
    harness.clock.advance(LEASE_SECONDS + 1)

    outcome = harness.executor(tasks, client, checks=checks).run()

    assert outcome.exit_code == 0
    collect_requests = [r for r in client.responses if r.task_id == "collect"]
    assert len(collect_requests) == 1
    assert collect_requests[0].idempotency_key != first_key
    assert client.request_status(first_key) is RequestStatus.NOT_SENT
    # 按规则重新派发：RECOVERY_REQUIRED 先回到门禁，再 READY -> CLAIMED
    states = [
        (e.previous_state, e.new_state)
        for e in replay_events(harness)
        if e.task_id == "collect" and e.new_state is not None
    ]
    i = states.index((TaskState.RUNNING, TaskState.RECOVERY_REQUIRED))
    assert states[i + 1 : i + 4] == [
        (TaskState.RECOVERY_REQUIRED, TaskState.FEASIBILITY_CHECK),
        (TaskState.FEASIBILITY_CHECK, TaskState.READY),
        (TaskState.READY, TaskState.CLAIMED),
    ]


@pytest.mark.parametrize("crash", [False, True])
def test_recovery_unknown_stays_recovery_required(harness, load_graph, crash):
    tasks, checks = declared(load_graph("01_linear.json").tasks)
    # 第二步 success 是“陷阱”：若执行器重发，就会被消费
    client = FakeClient([Step.timeout(), *ok_steps(2)])
    if crash:
        hook, fired = crash_once("collect")
        crash_first_run(harness, tasks, checks, client, after_send=hook)
        first_key = fired[0]
    else:
        first = harness.executor(tasks, client, checks=checks).run()
        assert first.exit_code != 0
        assert first.states["collect"] is TaskState.RECOVERY_REQUIRED
        first_key = client.responses[0].idempotency_key
    assert client.request_status(first_key) is RequestStatus.UNKNOWN
    harness.clock.advance(LEASE_SECONDS + 1)
    responses_before = client.responses
    remaining_before = client.script_remaining

    outcome = harness.executor(tasks, client, checks=checks).run()

    assert outcome.states["collect"] is TaskState.RECOVERY_REQUIRED
    assert outcome.states["report"] is TaskState.PENDING
    assert client.responses == responses_before  # 没有收到任何重复请求
    assert client.script_remaining == remaining_before
    assert client.attempts(first_key) == 1
    assert outcome.exit_code != 0
    kept = events_of(harness, "api_request_failed", "collect")
    assert any(e.details.get("request_status") == "unknown" for e in kept)


# ===================================================== 验收失败 → 总运行失败


def failing_acceptor(task, response):
    return AcceptanceReport(task.id, False, ("custom",), ("输出不符合要求",))


def missing_acceptor(task, response):
    return None


def raising_acceptor(task, response):
    raise RuntimeError("验收脚本崩溃")


@pytest.mark.parametrize(
    "acceptor, collect_state, reason",
    [
        (failing_acceptor, TaskState.FAILED, "验收失败"),
        (missing_acceptor, TaskState.RECOVERY_REQUIRED, "缺少验收报告"),
        (raising_acceptor, TaskState.RECOVERY_REQUIRED, "验收程序异常"),
    ],
)
def test_acceptance_problems_fail_the_run(
    harness, load_graph, acceptor, collect_state, reason
):
    tasks, checks = declared(load_graph("01_linear.json").tasks)
    client = FakeClient(ok_steps(2))
    outcome = harness.executor(tasks, client, checks=checks, acceptor=acceptor).run()

    assert client.responses[0].ok  # API 成功不等于任务成功
    assert outcome.states["collect"] is collect_state
    assert outcome.states["report"] is TaskState.PENDING  # 下游没有被释放
    assert not outcome.verdict.ok
    assert outcome.exit_code != 0
    assert any(reason in item for item in outcome.verdict.reasons)
    assert events_of(harness, "run_failed")


def test_api_success_with_invalid_output_fails_contract(harness, load_graph):
    tasks, checks = declared(load_graph("01_linear.json").tasks)
    client = FakeClient([Step.success('{"other": 1}'), *ok_steps(1)])
    outcome = harness.executor(tasks, client, checks=checks).run()

    assert outcome.states["collect"] is TaskState.FAILED
    assert outcome.reports["collect"].errors == ("缺少必需字段 'ok'",)
    assert outcome.exit_code != 0


def test_judge_crash_fails_the_run(harness, load_graph, monkeypatch):
    def broken_judge(**kwargs):
        raise RuntimeError("判定程序崩溃")

    monkeypatch.setattr(executor_module, "judge_run", broken_judge)
    tasks, checks = declared(load_graph("01_linear.json").tasks)
    outcome = harness.executor(tasks, FakeClient(ok_steps(2)), checks=checks).run()

    assert outcome.exit_code != 0
    assert any("验收程序异常" in reason for reason in outcome.verdict.reasons)
    assert events_of(harness, "run_failed")


def test_external_gate_not_passed_overrides_scheduler_completion(harness, load_graph):
    tasks, checks = declared(load_graph("01_linear.json").tasks)
    outcome = harness.executor(
        tasks, FakeClient(ok_steps(2)), checks=checks,
        external_gates={"IntegrationPass": None},
    ).run()

    assert outcome.verdict.scheduler_completed
    assert outcome.verdict.acceptance_passed
    assert not outcome.verdict.ok
    assert outcome.exit_code != 0


RUN_SCRIPT = textwrap.dedent(
    """
    import sys
    from feasibility_scheduler.acceptance import AcceptanceReport
    from feasibility_scheduler.events import EventLog
    from feasibility_scheduler.executor import Executor, exit_with
    from feasibility_scheduler.fake_client import FakeClient, Step
    from feasibility_scheduler.models import Constraint, Task
    from feasibility_scheduler.phi import ConstraintCheck
    from feasibility_scheduler.store import TaskStore

    mode, workdir = sys.argv[1], sys.argv[2]
    contract = {"type": "object", "required": ["ok"]}
    rule = (Constraint("contract_declared", True),)
    tasks = [
        Task(id="collect", constraints=rule, constraints_declared=True,
             output_contract=contract, payload={"work": True}),
        Task(id="report", dependencies=("collect",), constraints=rule,
             constraints_declared=True, output_contract=contract, payload={"work": True}),
    ]
    checks = {t.id: [ConstraintCheck("contract_declared", "SATISFIED")] for t in tasks}

    def acceptor(task, response):
        if mode == "fail":
            return AcceptanceReport(task.id, False, errors=("不符合",))
        if mode == "missing":
            return None
        if mode == "raise":
            raise RuntimeError("boom")
        return AcceptanceReport(task.id, True, ("ok",))

    store = TaskStore(workdir + "/state.sqlite3")
    log = EventLog(workdir + "/events.jsonl", run_id="e2e-run")
    client = FakeClient([Step.success('{"ok": true}')] * 2)
    outcome = Executor(tasks, store=store, log=log, client=client,
                       checks=checks, acceptor=acceptor).run()
    store.close()
    exit_with(outcome)
    """
)


@pytest.mark.parametrize(
    "mode, expect_zero", [("pass", True), ("fail", False), ("missing", False), ("raise", False)]
)
def test_process_exit_code_follows_run_verdict(harness, mode, expect_zero):
    script = harness.tmp / "run.py"
    script.write_text(RUN_SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script), mode, str(harness.tmp)],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (completed.returncode == 0) is expect_zero, completed.stderr
    # 非零码必须来自总运行判定，而不是脚本崩溃（崩溃同样返回 1）
    assert completed.returncode in (0, 1), completed.stderr
    assert "Traceback" not in completed.stderr, completed.stderr
    final = replay_events(harness)[-1]
    assert final.type == ("run_completed" if expect_zero else "run_failed")
