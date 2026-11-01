"""任务状态机：规格第 4.3 节的显式迁移表，未列入的迁移一律拒绝。

设计约束
--------
1. 迁移表是唯一权威：``TRANSITIONS`` 列出全部合法边，``transition`` 只认表中
   的 ``(source, target)`` 对，其余全部抛 ``IllegalTransition``。
2. 终态为 ``ACCEPTED``、``FAILED``、``CANCELLED``：三者没有出边。本任务目标
   明确以 ``ACCEPTED`` 为终态，因此不设 ``COMPLETED``；规格 4.3 示意图中的
   ``ACCEPTED -> COMPLETED`` 按该收敛决定处理，不计入本表。
3. 租约过期是独立入口 ``transition_on_lease_expiry``：只接受持有租约的状态
   （``CLAIMED``、``RUNNING``），且目标硬编码为 ``RECOVERY_REQUIRED``。
   ``RECOVERY_REQUIRED`` 没有直接回到 ``READY`` 的边，必须先重新走
   ``FEASIBILITY_CHECK``。
4. 任何非终态都可以 ``CANCELLED``；``FAILED`` 与 ``ACCEPTED`` 只从验收/执行
   分支进入，不能由取消代替。

表中 ``reason`` 是给人看的默认说明，写入事件日志时可直接复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping


class TaskState(str, Enum):
    """任务状态。值即持久化到 SQLite 的字符串。"""

    PENDING = "PENDING"
    FEASIBILITY_CHECK = "FEASIBILITY_CHECK"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    ACCEPTANCE_PENDING = "ACCEPTANCE_PENDING"
    ACCEPTED = "ACCEPTED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.ACCEPTED, TaskState.FAILED, TaskState.CANCELLED}
)

#: 持有执行租约的状态；只有这些状态可以“租约过期”。
LEASE_HOLDING_STATES: frozenset[TaskState] = frozenset(
    {TaskState.CLAIMED, TaskState.RUNNING}
)

#: 租约过期唯一允许的目标状态。
LEASE_EXPIRY_TARGET = TaskState.RECOVERY_REQUIRED


@dataclass(frozen=True)
class Transition:
    """一条合法迁移。"""

    source: TaskState
    target: TaskState
    reason: str


_TRANSITIONS: tuple[Transition, ...] = (
    # 初始可行性检查
    Transition(TaskState.PENDING, TaskState.FEASIBILITY_CHECK, "进入可行性检查"),
    Transition(TaskState.PENDING, TaskState.CANCELLED, "任务在开始前被取消"),
    # 门禁结论（gate.py：READY / BLOCKED / DEFERRED）
    Transition(TaskState.FEASIBILITY_CHECK, TaskState.READY, "门禁通过，任务就绪"),
    Transition(TaskState.FEASIBILITY_CHECK, TaskState.BLOCKED, "硬约束被违反，任务阻塞"),
    Transition(TaskState.FEASIBILITY_CHECK, TaskState.DEFERRED, "必要信息缺失，暂缓派发"),
    Transition(TaskState.FEASIBILITY_CHECK, TaskState.CANCELLED, "任务在检查阶段被取消"),
    # 条件变化后重新评估；不回退到 PENDING，避免丢失已产生的检查痕迹
    Transition(TaskState.BLOCKED, TaskState.FEASIBILITY_CHECK, "阻塞原因变化，重新检查"),
    Transition(TaskState.BLOCKED, TaskState.CANCELLED, "阻塞任务被取消"),
    Transition(TaskState.DEFERRED, TaskState.FEASIBILITY_CHECK, "补充信息后重新检查"),
    Transition(TaskState.DEFERRED, TaskState.CANCELLED, "暂缓任务被取消"),
    # 领取与执行
    Transition(TaskState.READY, TaskState.CLAIMED, "Worker 领取任务并持有租约"),
    Transition(TaskState.READY, TaskState.CANCELLED, "就绪任务被取消"),
    Transition(TaskState.CLAIMED, TaskState.RUNNING, "Worker 开始执行"),
    Transition(TaskState.CLAIMED, TaskState.RECOVERY_REQUIRED, "租约过期，进入恢复流程"),
    Transition(TaskState.CLAIMED, TaskState.READY, "Worker 未开始执行即主动释放领取"),
    Transition(TaskState.CLAIMED, TaskState.CANCELLED, "领取后、开始前被取消"),
    Transition(TaskState.RUNNING, TaskState.RESULT_RECEIVED, "收到执行结果"),
    Transition(TaskState.RUNNING, TaskState.RETRY_WAIT, "可重试失败，等待重试"),
    Transition(TaskState.RUNNING, TaskState.FAILED, "失败且不可重试"),
    Transition(TaskState.RUNNING, TaskState.RECOVERY_REQUIRED, "租约过期，进入恢复流程"),
    Transition(TaskState.RUNNING, TaskState.CANCELLED, "执行被取消"),
    # 独立验收
    Transition(TaskState.RESULT_RECEIVED, TaskState.ACCEPTANCE_PENDING, "结果交给独立验收"),
    Transition(TaskState.RESULT_RECEIVED, TaskState.REJECTED, "结果在受理阶段被拒绝"),
    Transition(TaskState.RESULT_RECEIVED, TaskState.RECOVERY_REQUIRED, "结果记录异常，进入恢复流程"),
    Transition(TaskState.RESULT_RECEIVED, TaskState.CANCELLED, "结果待验收时被取消"),
    Transition(TaskState.ACCEPTANCE_PENDING, TaskState.ACCEPTED, "独立验收通过"),
    Transition(TaskState.ACCEPTANCE_PENDING, TaskState.REJECTED, "独立验收不通过"),
    Transition(
        TaskState.ACCEPTANCE_PENDING,
        TaskState.RECOVERY_REQUIRED,
        "验收过程异常，进入恢复流程",
    ),
    Transition(TaskState.ACCEPTANCE_PENDING, TaskState.CANCELLED, "验收阶段被取消"),
    # 重试与拒绝后的去向
    Transition(TaskState.RETRY_WAIT, TaskState.CLAIMED, "重试等待结束，重新领取"),
    Transition(TaskState.RETRY_WAIT, TaskState.FAILED, "重试耗尽"),
    Transition(TaskState.RETRY_WAIT, TaskState.RECOVERY_REQUIRED, "等待重试期间状态异常"),
    Transition(TaskState.RETRY_WAIT, TaskState.CANCELLED, "等待重试期间被取消"),
    Transition(TaskState.REJECTED, TaskState.RETRY_WAIT, "拒绝后安排重试"),
    Transition(TaskState.REJECTED, TaskState.FAILED, "拒绝且不再重试"),
    Transition(TaskState.REJECTED, TaskState.RECOVERY_REQUIRED, "拒绝后状态需人工恢复"),
    Transition(TaskState.REJECTED, TaskState.CANCELLED, "被拒绝的任务不再重试并取消"),
    # 恢复流程：必须重新过门禁，不得直接 READY
    Transition(
        TaskState.RECOVERY_REQUIRED,
        TaskState.FEASIBILITY_CHECK,
        "恢复后重新执行可行性检查",
    ),
    Transition(TaskState.RECOVERY_REQUIRED, TaskState.FAILED, "恢复失败，任务终止"),
    Transition(TaskState.RECOVERY_REQUIRED, TaskState.CANCELLED, "恢复期间决定取消"),
)

#: ``(source, target) -> reason``，只读。
TRANSITION_REASONS: Mapping[tuple[TaskState, TaskState], str] = MappingProxyType(
    {(item.source, item.target): item.reason for item in _TRANSITIONS}
)

#: ``source -> 可达目标集合``，只读；终态对应空集合。
ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = MappingProxyType(
    {
        state: frozenset(
            target for (source, target) in TRANSITION_REASONS if source is state
        )
        for state in TaskState
    }
)


class StateMachineError(ValueError):
    """状态机相关错误的基类。"""


class IllegalTransition(StateMachineError):
    """迁移不在表中。"""

    def __init__(self, source: TaskState, target: TaskState) -> None:
        self.source = source
        self.target = target
        message = f"非法迁移：{source.value} -> {target.value}"
        if source in TERMINAL_STATES:
            message += f"（{source.value} 是终态）"
        else:
            allowed = ", ".join(sorted(item.value for item in ALLOWED_TRANSITIONS[source]))
            message += f"；{source.value} 只允许 -> [{allowed or '无'}]"
        super().__init__(message)

    @property
    def from_terminal(self) -> bool:
        return self.source in TERMINAL_STATES


class TerminalStateError(IllegalTransition):
    """终态之后的迁移。"""


class LeaseNotHeldError(StateMachineError):
    """状态不持有租约，不能按“租约过期”处理。"""

    def __init__(self, state: TaskState) -> None:
        self.state = state
        allowed = ", ".join(sorted(item.value for item in LEASE_HOLDING_STATES))
        super().__init__(
            f"{state.value} 不持有执行租约，租约过期只能从 [{allowed}] 进入 "
            f"{LEASE_EXPIRY_TARGET.value}"
        )


def parse_state(value: TaskState | str) -> TaskState:
    """把持久化字符串或枚举统一成 ``TaskState``。"""
    if isinstance(value, TaskState):
        return value
    try:
        return TaskState(value)
    except ValueError as exc:
        raise StateMachineError(f"未知任务状态：{value!r}") from exc


def is_terminal(state: TaskState | str) -> bool:
    return parse_state(state) in TERMINAL_STATES


def allowed_targets(state: TaskState | str) -> frozenset[TaskState]:
    return ALLOWED_TRANSITIONS[parse_state(state)]


def can_transition(source: TaskState | str, target: TaskState | str) -> bool:
    """仅按迁移表判断，不做业务前置条件检查。"""
    return parse_state(target) in ALLOWED_TRANSITIONS[parse_state(source)]


def check_transition(source: TaskState | str, target: TaskState | str) -> Transition:
    """校验并返回迁移；不合法时抛出 ``IllegalTransition``/``TerminalStateError``。"""
    source_state = parse_state(source)
    target_state = parse_state(target)
    try:
        reason = TRANSITION_REASONS[(source_state, target_state)]
    except KeyError:
        if source_state in TERMINAL_STATES:
            raise TerminalStateError(source_state, target_state) from None
        raise IllegalTransition(source_state, target_state) from None
    return Transition(source_state, target_state, reason)


def transition(source: TaskState | str, target: TaskState | str) -> TaskState:
    """返回迁移后的状态（即 ``target``），供调用方持久化。"""
    return check_transition(source, target).target


def transition_on_lease_expiry(source: TaskState | str) -> TaskState:
    """租约过期：只允许 ``CLAIMED``/``RUNNING`` -> ``RECOVERY_REQUIRED``。"""
    source_state = parse_state(source)
    if source_state not in LEASE_HOLDING_STATES:
        raise LeaseNotHeldError(source_state)
    # 仍走统一校验，避免表被改动后这里成为绕过点。
    return transition(source_state, LEASE_EXPIRY_TARGET)


def recoverable_states() -> frozenset[TaskState]:
    """可以进入 ``RECOVERY_REQUIRED`` 的状态（按表推导，非硬编码清单）。"""
    return frozenset(
        source
        for (source, target) in TRANSITION_REASONS
        if target is TaskState.RECOVERY_REQUIRED
    )


def iter_transitions() -> Iterable[Transition]:
    return _TRANSITIONS
