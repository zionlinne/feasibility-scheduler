"""从 JSONL 事件日志重放出任务状态（规格第 7 节、4.3 节）。

重放是**严格**的：

1. 只接受单个 run 的事件，``sequence`` 必须严格递增，``event_id`` 不得重复；
2. 每个带 ``new_state`` 的事件都必须能被 :mod:`feasibility_scheduler.statemachine`
   的迁移表解释；
3. 任务的首次状态必须是 ``PENDING``，且 ``previous_state`` 必须与重放得到的
   当前状态一致；``previous_state`` 与 ``new_state`` 不能只给一个；
4. 任何违反上述规则的事件都会抛 :class:`ReplayError`，错误信息包含
   event ID、任务与迁移边，绝不跳过该事件继续重放。

终态（``ACCEPTED`` / ``FAILED`` / ``CANCELLED``）之后再出现状态事件同样会
被迁移表拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .events import Event, EventLogError, read_events
from .statemachine import (
    TaskState,
    IllegalTransition,
    check_transition,
)


class ReplayError(Exception):
    """事件序列无法按迁移表重放。"""


@dataclass(frozen=True)
class ReplayedTask:
    """单个任务的重放轨迹。"""

    task_id: str
    state: TaskState
    event_ids: tuple[str, ...]
    transitions: tuple[tuple[TaskState | None, TaskState], ...]


@dataclass(frozen=True)
class ReplayResult:
    """整份日志的重放结果。"""

    run_id: str
    tasks: Mapping[str, ReplayedTask]
    event_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", MappingProxyType(dict(self.tasks)))

    def state_of(self, task_id: str) -> TaskState:
        try:
            return self.tasks[task_id].state
        except KeyError as exc:
            raise ReplayError(f"日志中没有任务 {task_id!r} 的状态") from exc

    @property
    def all_terminal(self) -> bool:
        return bool(self.tasks) and all(
            task.state
            in {TaskState.ACCEPTED, TaskState.FAILED, TaskState.CANCELLED}
            for task in self.tasks.values()
        )


def replay(events: Iterable[Event]) -> ReplayResult:
    """按顺序重放事件；遇到第一条违规事件立即报错。"""
    ordered = tuple(events)
    if not ordered:
        raise ReplayError("事件日志为空，无法重放任务状态")

    run_id = ordered[0].run_id
    if not isinstance(run_id, str) or not run_id:
        raise ReplayError("首条事件缺少 run_id")

    seen_event_ids: set[str] = set()
    previous_sequence: int | None = None
    states: dict[str, TaskState] = {}
    event_ids: dict[str, list[str]] = {}
    transitions: dict[str, list[tuple[TaskState | None, TaskState]]] = {}

    for event in ordered:
        if event.run_id != run_id:
            raise ReplayError(
                f"事件 {event.event_id} 属于 run {event.run_id!r}，"
                f"与 run {run_id!r} 混在同一份日志"
            )
        if event.event_id in seen_event_ids:
            raise ReplayError(f"event_id 重复：{event.event_id}")
        if previous_sequence is not None and event.sequence <= previous_sequence:
            raise ReplayError(
                f"事件 {event.event_id} 的 sequence {event.sequence} 未严格递增"
                f"（上一条为 {previous_sequence}）"
            )
        seen_event_ids.add(event.event_id)
        previous_sequence = event.sequence

        new_state = event.new_state
        previous_state = event.previous_state
        if new_state is None:
            if previous_state is not None:
                raise ReplayError(
                    f"事件 {event.event_id} 有 previous_state={previous_state.value} "
                    "却没有 new_state，状态事件不完整"
                )
            continue

        task_id = event.task_id
        if task_id is None:
            raise ReplayError(f"事件 {event.event_id} 带有状态但缺少 task_id")

        current = states.get(task_id)
        if previous_state is None:
            if current is not None:
                raise ReplayError(
                    f"事件 {event.event_id} 缺少 previous_state，"
                    f"但任务 {task_id!r} 已处于 {current.value}"
                )
            if new_state is not TaskState.PENDING:
                raise ReplayError(
                    f"任务 {task_id!r} 首次出现必须是 PENDING，"
                    f"事件 {event.event_id} 却给出 {new_state.value}"
                )
        else:
            if current is None:
                raise ReplayError(
                    f"任务 {task_id!r} 首次出现却带 previous_state="
                    f"{previous_state.value}（事件 {event.event_id}）"
                )
            if current is not previous_state:
                raise ReplayError(
                    f"事件 {event.event_id} 声称从 {previous_state.value} 出发，"
                    f"但任务 {task_id!r} 当前状态是 {current.value}（不一致）"
                )
            try:
                check_transition(current, new_state)
            except IllegalTransition as exc:
                raise ReplayError(
                    f"事件 {event.event_id} 违反迁移表："
                    f"任务 {task_id!r} {current.value} -> {new_state.value}"
                ) from exc

        states[task_id] = new_state
        event_ids.setdefault(task_id, []).append(event.event_id)
        transitions.setdefault(task_id, []).append((previous_state, new_state))

    tasks = {
        task_id: ReplayedTask(
            task_id=task_id,
            state=state,
            event_ids=tuple(event_ids[task_id]),
            transitions=tuple(transitions[task_id]),
        )
        for task_id, state in states.items()
    }
    return ReplayResult(run_id=run_id, tasks=tasks, event_count=len(ordered))


def replay_file(path: str | Path) -> ReplayResult:
    """读取 JSONL 并重放；读取或迁移错误统一抛 :class:`ReplayError`。"""
    try:
        events = read_events(path)
    except EventLogError as exc:
        raise ReplayError(f"事件日志无法读取：{exc}") from exc
    return replay(events)
