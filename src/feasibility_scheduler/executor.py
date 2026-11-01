"""有界并发执行器（规格第 4.2、4.3、5 节）。

调度规则
--------
1. **只派发 READY**：任务先由 Φ（以及可选的时间分析门禁）判定为 READY 才会被
   领取；BLOCKED / DEFERRED 不派发。派发前再核对一次：任务必须是 READY，
   全部直接上游必须是 ``ACCEPTED``，否则抛 :class:`DispatchInvariantError`。
2. **只在上游 ACCEPTED 后释放下游**：``RESULT_RECEIVED``、``ACCEPTANCE_PENDING``
   等中间状态都不释放下游；上游失败时下游保持 ``PENDING``，不派发。
3. **有界并发**：同时执行的任务数不超过 ``max_concurrency``；每个执行中的任务
   占用一个编号执行槽（``slot-N``），执行槽不是独立 Agent。
4. **双写一致**：每次状态迁移先写状态存储，再把同一条迁移写入 JSONL 事件日志，
   两步之间没有 ``await``；测试用的崩溃注入点只在两步之外。

恢复规则
--------
新进程启动时，租约已过期的 ``CLAIMED`` / ``RUNNING``，以及无人持有的
``RESULT_RECEIVED`` / ``ACCEPTANCE_PENDING`` / ``RETRY_WAIT`` 先进入
``RECOVERY_REQUIRED``，再按最后一次请求的幂等键向 client 查询：

- ``sent``：不再发请求，回收已有响应，重新过门禁后走独立验收；
- ``not_sent``：重新过门禁，按正常规则派发（使用新的幂等键）；
- ``unknown``：保持 ``RECOVERY_REQUIRED``，不发请求，等待人工处理。

请求超时（provider 可能已经处理）同样进入 ``RECOVERY_REQUIRED``，不自动重发。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Iterable, Mapping, NoReturn

from .acceptance import (
    AcceptanceReport,
    Acceptor,
    RunVerdict,
    contract_acceptor,
    judge_run,
    run_acceptance,
)
from .dag import TaskGraph, build_dag
from .events import EventLog
from .client import SchedulerClient
from .fake_client import (
    DEFAULT_MODEL,
    FAKE_PROVIDER,
    ApiRequest,
    ApiResponse,
    Outcome,
    RequestStatus,
)
from .gate import Decision, gate_task
from .models import Task
from .phi import ConstraintCheck, Truth, evaluate
from .statemachine import LEASE_HOLDING_STATES, TaskState
from .store import TaskNotFoundError, TaskStore
from .timing import TimingReport
from .validation import validate_tasks

COMPONENT = "executor"

_DECISION_STATE = {
    Decision.READY: TaskState.READY,
    Decision.BLOCKED: TaskState.BLOCKED,
    Decision.DEFERRED: TaskState.DEFERRED,
}
_DECISION_EVENT = {
    Decision.READY: "task_ready",
    Decision.BLOCKED: "task_blocked",
    Decision.DEFERRED: "task_deferred",
}
_PHI_DECISION = {
    Truth.TRUE: Decision.READY,
    Truth.FALSE: Decision.BLOCKED,
    Truth.UNKNOWN: Decision.DEFERRED,
}

#: 不持有租约、但结果尚未落定的中间状态；进程重启后无人负责，需进入恢复流程。
_UNOWNED_IN_PROGRESS = frozenset(
    {TaskState.RESULT_RECEIVED, TaskState.ACCEPTANCE_PENDING, TaskState.RETRY_WAIT}
)

TransitionHook = Callable[[str, "TaskState | None", TaskState], Awaitable[None]]
SendHook = Callable[[ApiRequest], Awaitable[None]]


class DispatchInvariantError(RuntimeError):
    """派发前核对失败：任务不是 READY，或存在未 ACCEPTED 的上游。"""


class ActiveLeaseError(RuntimeError):
    """恢复时发现租约仍有效的任务，可能有另一个进程在执行，拒绝接管。"""


@dataclass(frozen=True)
class RunOutcome:
    """一次 ``run`` 的结果；``exit_code`` 来自总运行判定。"""

    verdict: RunVerdict
    states: Mapping[str, TaskState]
    reports: Mapping[str, AcceptanceReport]
    max_in_flight: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "states", MappingProxyType(dict(self.states)))
        object.__setattr__(self, "reports", MappingProxyType(dict(self.reports)))

    @property
    def ok(self) -> bool:
        return self.verdict.ok

    @property
    def exit_code(self) -> int:
        return self.verdict.exit_code


def exit_with(outcome: RunOutcome) -> NoReturn:
    """以总运行判定的退出码结束进程：失败必为非零。"""
    raise SystemExit(outcome.exit_code)


class Executor:
    """在 ``TaskStore`` + ``EventLog`` 之上驱动一张任务图的执行与验收。"""

    def __init__(
        self,
        tasks: Iterable[Task],
        *,
        store: TaskStore,
        log: EventLog,
        client: SchedulerClient,
        acceptor: Acceptor = contract_acceptor,
        max_concurrency: int = 1,
        checks: Mapping[str, Iterable[ConstraintCheck]] | None = None,
        timing: TimingReport | None = None,
        max_attempts: int = 3,
        lease_seconds: float = 60,
        external_gates: Mapping[str, bool | None] | None = None,
        on_transition: TransitionHook | None = None,
        before_send: SendHook | None = None,
        after_send: SendHook | None = None,
    ) -> None:
        if not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency 必须是正整数")
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts 必须是正整数")
        self.tasks = tuple(tasks)
        self.store = store
        self.log = log
        self.client = client
        self.acceptor = acceptor
        self.max_concurrency = max_concurrency
        self.checks = {key: tuple(value) for key, value in (checks or {}).items()}
        self.timing = timing
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.external_gates = dict(external_gates or {})
        self.on_transition = on_transition
        self.before_send = before_send
        self.after_send = after_send

        self.graph: TaskGraph | None = None
        self.reports: dict[str, AcceptanceReport] = {}
        #: 每次派发时记录 ``(task_id, {上游: 状态})``，供测试核对释放顺序。
        self.dispatch_log: list[tuple[str, dict[str, TaskState]]] = []
        #: 调度循环执行释放检查的次数。
        self.release_passes = 0
        self.max_in_flight = 0
        self._active = 0
        self._slots: set[int] = set()
        self._running: set[str] = set()
        self._recovered: dict[str, ApiResponse] = {}
        self._task_by_id = {task.id: task for task in self.tasks}

    # ------------------------------------------------------------- 入口

    def run(self) -> RunOutcome:
        return asyncio.run(self.run_async())

    async def run_async(self) -> RunOutcome:
        if not any(event.type == "run_created" for event in self.log.events()):
            self.log.record(
                "run_created",
                component=COMPONENT,
                decision_reason="创建运行",
                details={
                    "task_ids": sorted(self._task_by_id),
                    "max_concurrency": self.max_concurrency,
                    "max_attempts": self.max_attempts,
                },
            )
        errors = list(validate_tasks(self.tasks))
        if not self.tasks:
            errors.append("任务图为空")
        if errors:
            return self._finish(graph_errors=errors)
        self.graph = build_dag(self.tasks)
        self._register()
        self._load_reports()
        await self._recover()
        await self._schedule()
        return self._finish()

    # ------------------------------------------------------------- 调度循环

    async def _schedule(self) -> None:
        inflight: set[asyncio.Task[None]] = set()
        try:
            while True:
                ready = await self._release()
                for task_id in ready:
                    if len(inflight) >= self.max_concurrency:
                        break
                    self._assert_dispatchable(task_id)
                    self._running.add(task_id)
                    inflight.add(asyncio.ensure_future(self._worker(task_id)))
                if not inflight:
                    return
                done, _ = await asyncio.wait(
                    inflight, return_when=asyncio.FIRST_COMPLETED
                )
                for future in done:
                    inflight.discard(future)
                    future.result()  # 崩溃注入或程序错误直接向上抛出
        finally:
            for future in inflight:
                future.cancel()
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)

    async def _release(self) -> list[str]:
        """把上游全部 ACCEPTED 的 PENDING 任务送入门禁；返回待派发的 READY 任务。"""
        assert self.graph is not None
        self.release_passes += 1
        ready: list[str] = []
        for task_id in self.graph.topological_order():
            state = self.store.get_task(task_id).state
            if state is TaskState.PENDING and self._upstream_accepted(task_id):
                await self._gate(task_id)
                state = self.store.get_task(task_id).state
            if state is TaskState.READY and task_id not in self._running:
                ready.append(task_id)
        return ready

    def _upstream_accepted(self, task_id: str) -> bool:
        assert self.graph is not None
        return all(
            self.store.get_task(dep).state is TaskState.ACCEPTED
            for dep in self.graph.predecessors_of(task_id)
        )

    def _assert_dispatchable(self, task_id: str) -> None:
        assert self.graph is not None
        record = self.store.get_task(task_id)
        upstream = {
            dep: self.store.get_task(dep).state
            for dep in self.graph.predecessors_of(task_id)
        }
        if record.state is not TaskState.READY:
            raise DispatchInvariantError(
                f"任务 {task_id!r} 状态为 {record.state.value}，只有 READY 可以派发"
            )
        pending = sorted(dep for dep, state in upstream.items() if state is not TaskState.ACCEPTED)
        if pending:
            raise DispatchInvariantError(
                f"任务 {task_id!r} 的上游 {pending} 尚未 ACCEPTED，禁止派发"
            )
        self.dispatch_log.append((task_id, upstream))

    async def _gate(self, task_id: str) -> Decision:
        """PENDING / RECOVERY_REQUIRED -> FEASIBILITY_CHECK -> READY/BLOCKED/DEFERRED。"""
        task = self._task_by_id[task_id]
        report = evaluate(task, self.checks.get(task_id, ()))
        if self.timing is not None:
            result = gate_task(report, self.timing[task_id])
            decision, truth, reasons = result.decision, result.phi, result.reasons
        else:
            decision, truth, reasons = _PHI_DECISION[report.truth], report.truth, report.reasons
        await self._step(
            task_id,
            TaskState.FEASIBILITY_CHECK,
            "phi_evaluated",
            report.describe(),
            details={"phi": truth.value, "decision": decision.value, "reasons": list(reasons)},
        )
        await self._step(
            task_id,
            _DECISION_STATE[decision],
            _DECISION_EVENT[decision],
            "；".join(reasons) or decision.value,
        )
        return decision

    # ------------------------------------------------------------- 执行槽

    async def _worker(self, task_id: str) -> None:
        slot = self._take_slot()
        self._active += 1
        self.max_in_flight = max(self.max_in_flight, self._active)
        try:
            while True:
                key = await self._claim(task_id, slot)
                await self._step(
                    task_id, TaskState.RUNNING, "api_request_started",
                    f"{slot} 开始执行", idempotency_key=key,
                )
                recovered = self._recovered.pop(task_id, None)
                if recovered is not None:
                    response = recovered
                    self._record(
                        "api_response_received", task_id,
                        "回收恢复前已发出请求的响应，不重复发送",
                        details={**response.to_audit_dict(), "recovered": True},
                    )
                else:
                    response = await self._send(task_id, key)
                if await self._handle_response(task_id, response):
                    return
        finally:
            self._active -= 1
            self._release_slot(slot)
            self._running.discard(task_id)

    async def _claim(self, task_id: str, slot: str) -> str:
        record = self.store.get_task(task_id)
        key = f"{self.log.run_id}:{task_id}:attempt-{record.attempts + 1}"
        claimed = self.store.claim(
            task_id, slot, idempotency_key=key, lease_seconds=self.lease_seconds
        )
        self._record(
            "task_claimed", task_id,
            f"{slot} 领取任务（第 {claimed.attempts} 次尝试）",
            previous_state=record.state, new_state=TaskState.CLAIMED,
            details={"slot": slot, "idempotency_key": key, "attempt": claimed.attempts},
        )
        await self._notify(task_id, record.state, TaskState.CLAIMED)
        return key

    async def _send(self, task_id: str, key: str) -> ApiResponse:
        request = ApiRequest(
            task_id=task_id,
            idempotency_key=key,
            prompt=self._prompt(task_id),
            model=getattr(self.client, "model", DEFAULT_MODEL),
            provider=getattr(self.client, "provider", FAKE_PROVIDER),
        )
        if self.before_send is not None:
            await self.before_send(request)
        response = await self.client.acall(request)
        if self.after_send is not None:
            await self.after_send(request)
        self._record(
            "api_response_received" if response.ok else "api_request_failed",
            task_id,
            f"provider 返回 {response.outcome.value}",
            details=response.to_audit_dict(),
        )
        return response

    async def _handle_response(self, task_id: str, response: ApiResponse) -> bool:
        """返回 True 表示任务本轮执行结束；False 表示已进入重试等待。"""
        if response.ok:
            await self._step(
                task_id, TaskState.RESULT_RECEIVED, "artifact_created",
                f"收到响应 {response.request_id}",
                details={"request_id": response.request_id},
            )
            await self._accept(task_id, response)
            return True
        if response.outcome is Outcome.TIMEOUT:
            await self._step(
                task_id, TaskState.RECOVERY_REQUIRED, "api_request_failed",
                "请求超时，provider 可能已处理，不自动重发",
                details={"idempotency_key": response.idempotency_key},
            )
            return True
        if response.retryable:
            attempts = self.store.get_task(task_id).attempts
            if attempts >= self.max_attempts:
                await self._step(
                    task_id, TaskState.FAILED, "api_request_failed",
                    f"重试耗尽（{attempts}/{self.max_attempts}）：{response.outcome.value}",
                )
                return True
            await self._step(
                task_id, TaskState.RETRY_WAIT, "retry_scheduled",
                f"{response.outcome.value}，安排第 {attempts + 1} 次尝试",
                details={"retry_after_seconds": response.retry_after_seconds},
            )
            return False
        await self._step(
            task_id, TaskState.FAILED, "api_request_failed",
            f"响应不可用且不可重试：{response.outcome.value}",
        )
        return True

    # ------------------------------------------------------------- 独立验收

    async def _accept(self, task_id: str, response: ApiResponse) -> None:
        await self._step(
            task_id, TaskState.ACCEPTANCE_PENDING, "acceptance_started", "交给独立验收"
        )
        report = run_acceptance(self.acceptor, self._task_by_id[task_id], response)
        if report is None:
            self.reports.pop(task_id, None)
            await self._step(
                task_id, TaskState.RECOVERY_REQUIRED, "acceptance_failed",
                "验收程序没有产出报告", details={"report": None},
            )
            return
        self.reports[task_id] = report
        details = {"report": report.to_dict()}
        if report.error:
            await self._step(
                task_id, TaskState.RECOVERY_REQUIRED, "acceptance_failed",
                report.error, details=details,
            )
        elif report.passed:
            await self._step(
                task_id, TaskState.ACCEPTED, "acceptance_passed",
                "独立验收通过", details=details,
            )
        else:
            reason = "；".join(report.errors) or "验收不通过"
            await self._step(
                task_id, TaskState.REJECTED, "acceptance_failed", reason, details=details
            )
            await self._step(
                task_id, TaskState.FAILED, "acceptance_failed", "验收不通过，任务终止"
            )

    def _load_reports(self) -> None:
        """从事件日志恢复此前写入的验收报告（进程重启后仍然可判定）。"""
        for event in self.log.events():
            if event.type not in {"acceptance_passed", "acceptance_failed"}:
                continue
            if event.task_id is None or "report" not in event.details:
                continue
            data = event.details["report"]
            if data is None:
                self.reports.pop(event.task_id, None)
            else:
                self.reports[event.task_id] = AcceptanceReport.from_dict(data)

    # ------------------------------------------------------------- 恢复

    async def _recover(self) -> None:
        assert self.graph is not None
        before = {record.task_id: record.state for record in self.store.list_tasks()}
        for record in self.store.expire_leases():
            self._record(
                "api_request_failed", record.task_id,
                "租约过期，执行槽失联，进入恢复流程",
                previous_state=before[record.task_id], new_state=record.state,
                details={"idempotency_key": record.idempotency_key, "recovery": True},
            )
            await self._notify(record.task_id, before[record.task_id], record.state)
        for task_id in self.graph.topological_order():
            state = self.store.get_task(task_id).state
            if state in LEASE_HOLDING_STATES:
                raise ActiveLeaseError(f"任务 {task_id!r} 的租约仍然有效，拒绝接管")
            if state in _UNOWNED_IN_PROGRESS:
                await self._step(
                    task_id, TaskState.RECOVERY_REQUIRED, "api_request_failed",
                    f"进程中断时停在 {state.value}，进入恢复流程",
                    details={"recovery": True},
                )
        for task_id in self.graph.topological_order():
            if self.store.get_task(task_id).state is TaskState.RECOVERY_REQUIRED:
                await self._resolve_recovery(task_id)

    async def _resolve_recovery(self, task_id: str) -> None:
        key = self.store.get_task(task_id).idempotency_key
        status = (
            self.client.request_status(key) if key is not None else RequestStatus.NOT_SENT
        )
        response = None
        if status is RequestStatus.SENT:
            response = self._lookup_sent_response(key)
        if status is RequestStatus.UNKNOWN or (
            status is RequestStatus.SENT and response is None
        ):
            self._record(
                "api_request_failed", task_id,
                f"幂等键 {key} 的请求状态为 {status.value}，无法确认 provider 是否处理；"
                "保持 RECOVERY_REQUIRED，不重发",
                details={"idempotency_key": key, "request_status": status.value},
            )
            return
        if not self._upstream_accepted(task_id):
            return
        decision = await self._gate(task_id)
        if decision is Decision.READY and response is not None:
            self._recovered[task_id] = response

    def _lookup_sent_response(self, key: str) -> ApiResponse | None:
        """按幂等键取回已发出请求的首次响应；client 可提供 ``lookup_response``。"""
        lookup = getattr(self.client, "lookup_response", None)
        if callable(lookup):
            return lookup(key)
        for response in reversed(getattr(self.client, "responses", ())):
            if response.idempotency_key == key and not response.is_duplicate:
                return response
        return None

    # ------------------------------------------------------------- 收尾

    def _finish(self, graph_errors: Iterable[str] = ()) -> RunOutcome:
        errors = tuple(graph_errors)
        task_ids = tuple(dict.fromkeys(task.id for task in self.tasks))
        try:
            verdict = judge_run(
                task_ids=task_ids,
                store=self.store,
                log_events=self.log.events(),
                reports=self.reports,
                graph_errors=errors,
                external_gates=self.external_gates,
            )
        except Exception as exc:  # 判定程序异常同样是失败，不是成功
            verdict = RunVerdict.failure(f"验收程序异常：{type(exc).__name__}: {exc}")
        self.log.record(
            "run_completed" if verdict.ok else "run_failed",
            component=COMPONENT,
            decision_reason="；".join(verdict.reasons) or "全部任务独立验收通过",
            details=verdict.to_dict(),
        )
        states: dict[str, TaskState] = {}
        for task_id in task_ids:
            try:
                states[task_id] = self.store.get_task(task_id).state
            except TaskNotFoundError:
                continue
        return RunOutcome(
            verdict=verdict,
            states=states,
            reports=self.reports,
            max_in_flight=self.max_in_flight,
        )

    # ------------------------------------------------------------- 内部

    def _register(self) -> None:
        assert self.graph is not None
        for task_id in self.graph.topological_order():
            try:
                self.store.get_task(task_id)
            except TaskNotFoundError:
                self.store.create_task(task_id)
                self._record(
                    "task_validated", task_id, "输入校验通过，登记任务",
                    previous_state=None, new_state=TaskState.PENDING,
                )

    async def _step(
        self,
        task_id: str,
        target: TaskState,
        event_type: str,
        reason: str,
        *,
        idempotency_key: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """状态存储与事件日志同步写入同一条迁移，然后通知观察者。"""
        previous = self.store.get_task(task_id).state
        self.store.transition(
            task_id, target, reason=reason, idempotency_key=idempotency_key
        )
        self._record(
            event_type, task_id, reason,
            previous_state=previous, new_state=target, details=details,
        )
        await self._notify(task_id, previous, target)

    def _record(
        self,
        event_type: str,
        task_id: str,
        reason: str,
        *,
        previous_state: TaskState | None = None,
        new_state: TaskState | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.log.record(
            event_type,
            component=COMPONENT,
            task_id=task_id,
            previous_state=previous_state,
            new_state=new_state,
            decision_reason=reason,
            details=details,
            task_input_hash=self._input_hash(task_id),
        )

    async def _notify(
        self, task_id: str, previous: TaskState | None, target: TaskState
    ) -> None:
        if self.on_transition is not None:
            await self.on_transition(task_id, previous, target)

    def _prompt(self, task_id: str) -> str:
        task = self._task_by_id[task_id]
        return json.dumps(
            {"task_id": task.id, "payload": task.payload},
            ensure_ascii=False, sort_keys=True, default=str,
        )

    def _input_hash(self, task_id: str) -> str:
        if task_id not in self._task_by_id:
            return ""
        digest = hashlib.sha256(self._prompt(task_id).encode("utf-8")).hexdigest()
        return "sha256:" + digest

    def _take_slot(self) -> str:
        index = 0
        while index in self._slots:
            index += 1
        self._slots.add(index)
        return f"slot-{index}"

    def _release_slot(self, slot: str) -> None:
        self._slots.discard(int(slot.rsplit("-", 1)[1]))
