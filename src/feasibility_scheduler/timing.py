"""DAG 时间分析：正向 / 反向传播、slack 与关键路径。

对应规格第 3.3 至 3.6 节。

模型定义
--------
- 时间是统一单位的数值（调用方负责单位换算），图的起始时刻为 `release`（默认 0）。
- 时长：每个任务可带估计时长与实测时长。有实测时用实测，否则用估计；
  结果中记录所用时长的来源（`estimated` / `actual`），两者不混为一谈。
  缺少任何可用时长时抛 `MissingDurationError`，不补默认值。
- 正向：`ES(v) = max(release, max EF(u) for u in 直接依赖)`，`EF = ES + d`。
- 反向：`deadline(v)` 是 `v` 的最晚完成时间。
  `LF(v) = min(deadline(v), min LS(w) for w in 直接后继)`，只取有限项；
  所有项都不存在时 `LF(v)` 与 `LS(v)` 为 `None`，表示无有限上界、需要外部 horizon，
  不制造 deadline，也不用无穷大或 0 冒充。`LS = LF - d`。
- `total_slack = LS - ES`，`LS` 为 `None` 时 slack 也为 `None`。
  slack 为负（`LS < ES`）表示在当前依赖与时长下无可行启动安排。
- `remaining_slack = LS - now`，与 `total_slack` 分开报告；未给 `now` 时为 `None`。
- 关键路径：沿“依赖的 EF 恰好等于本任务 ES”的紧边，从 EF 最大的终点回溯到起点，
  并列时取 ID 最小者。它只依赖正向结果，不需要 deadline。

本模块是纯函数：依赖或时长改变后，用新图 / 新时长重新调用 `analyze` 即得重算结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from types import MappingProxyType
from typing import Iterable, Mapping, Union

from .dag import TaskGraph, build_dag
from .models import DAG, Task

SOURCE_ESTIMATED = "estimated"
SOURCE_ACTUAL = "actual"


class TimingError(ValueError):
    """时间分析输入不合法。"""


class MissingDurationError(TimingError):
    """任务既没有实测时长也没有估计时长。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"缺少时长（估计与实测均未提供）: {task_id}")


@dataclass(frozen=True)
class Duration:
    """估计时长与实测时长分开存放。"""

    estimated: float | None = None
    actual: float | None = None

    def __post_init__(self) -> None:
        for label, value in (("estimated", self.estimated), ("actual", self.actual)):
            if value is not None and (not _is_number(value) or value < 0):
                raise TimingError(f"时长必须是非负数值: {label}={value!r}")

    @property
    def value(self) -> float | None:
        return self.actual if self.actual is not None else self.estimated

    @property
    def source(self) -> str | None:
        if self.actual is not None:
            return SOURCE_ACTUAL
        if self.estimated is not None:
            return SOURCE_ESTIMATED
        return None


DurationInput = Union[Duration, float, int]


@dataclass(frozen=True)
class TaskTiming:
    """单个任务的时间量。`None` 表示没有有限边界。"""

    task_id: str
    duration: float
    duration_source: str
    earliest_start: float
    earliest_finish: float
    latest_start: float | None
    latest_finish: float | None
    total_slack: float | None
    remaining_slack: float | None
    deadline: float | None

    @property
    def start_window(self) -> tuple[float, float] | None:
        """可行启动窗口 `[ES, LS]`；无上界或窗口为空时为 `None`。"""
        if self.latest_start is None or self.latest_start < self.earliest_start:
            return None
        return (self.earliest_start, self.latest_start)

    @property
    def infeasible(self) -> bool:
        """`LS < ES`：当前依赖与时长下已无法按 deadline 启动。"""
        return self.latest_start is not None and self.latest_start < self.earliest_start


@dataclass(frozen=True)
class TimingReport:
    """一次时间分析的完整结果。"""

    order: tuple[str, ...]
    timings: Mapping[str, TaskTiming]
    critical_path: tuple[str, ...]
    makespan: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timings", MappingProxyType(dict(self.timings)))

    def __getitem__(self, task_id: str) -> TaskTiming:
        return self.timings[task_id]

    @property
    def infeasible_tasks(self) -> tuple[str, ...]:
        return tuple(task_id for task_id in self.order if self.timings[task_id].infeasible)

    @property
    def feasible(self) -> bool:
        return not self.infeasible_tasks


def analyze(
    source: TaskGraph | DAG | Iterable[Task],
    durations: Mapping[str, DurationInput],
    deadlines: Mapping[str, float] | None = None,
    *,
    release: float = 0,
    now: float | None = None,
) -> TimingReport:
    """对任务图做正向与反向时间传播。

    `durations` 必须覆盖每个任务；`deadlines` 只需列出有最晚完成时间的任务。
    """
    graph = source if isinstance(source, TaskGraph) else build_dag(source)
    deadlines = dict(deadlines or {})
    unknown = sorted((set(durations) | set(deadlines)) - set(graph.task_ids))
    if unknown:
        raise TimingError("时长或 deadline 引用了图中不存在的任务: " + "、".join(unknown))
    for task_id, value in deadlines.items():
        if not _is_number(value):
            raise TimingError(f"deadline 必须是数值: {task_id}={value!r}")
    if not _is_number(release) or (now is not None and not _is_number(now)):
        raise TimingError("release / now 必须是数值")

    resolved = {task_id: _resolve_duration(task_id, durations) for task_id in graph.task_ids}
    order = graph.topological_order()

    earliest_start: dict[str, float] = {}
    earliest_finish: dict[str, float] = {}
    for task_id in order:
        start = max(
            [release] + [earliest_finish[dep] for dep in graph.predecessors_of(task_id)]
        )
        earliest_start[task_id] = start
        earliest_finish[task_id] = start + resolved[task_id].value

    latest_start: dict[str, float | None] = {}
    latest_finish: dict[str, float | None] = {}
    for task_id in reversed(order):
        bounds = [deadlines[task_id]] if task_id in deadlines else []
        bounds.extend(
            latest_start[succ]
            for succ in graph.successors_of(task_id)
            if latest_start[succ] is not None
        )
        finish = min(bounds) if bounds else None
        latest_finish[task_id] = finish
        latest_start[task_id] = None if finish is None else finish - resolved[task_id].value

    timings = {}
    for task_id in order:
        ls = latest_start[task_id]
        timings[task_id] = TaskTiming(
            task_id=task_id,
            duration=resolved[task_id].value,
            duration_source=resolved[task_id].source,
            earliest_start=earliest_start[task_id],
            earliest_finish=earliest_finish[task_id],
            latest_start=ls,
            latest_finish=latest_finish[task_id],
            total_slack=None if ls is None else ls - earliest_start[task_id],
            remaining_slack=None if ls is None or now is None else ls - now,
            deadline=deadlines.get(task_id),
        )

    makespan = max(earliest_finish.values(), default=release)
    return TimingReport(
        order=order,
        timings=timings,
        critical_path=_critical_path(graph, order, earliest_start, earliest_finish),
        makespan=makespan,
    )


# ---------------------------------------------------------------------- 内部


def _is_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and value == value


def _resolve_duration(task_id: str, durations: Mapping[str, DurationInput]) -> Duration:
    raw = durations.get(task_id)
    if raw is None:
        raise MissingDurationError(task_id)
    duration = raw if isinstance(raw, Duration) else Duration(estimated=raw)
    if duration.value is None:
        raise MissingDurationError(task_id)
    return duration


def _critical_path(
    graph: TaskGraph,
    order: tuple[str, ...],
    earliest_start: Mapping[str, float],
    earliest_finish: Mapping[str, float],
) -> tuple[str, ...]:
    if not order:
        return ()
    makespan = max(earliest_finish.values())
    current = min(task_id for task_id in order if earliest_finish[task_id] == makespan)
    path = [current]
    while True:
        tight = [
            dep
            for dep in graph.predecessors_of(current)
            if earliest_finish[dep] == earliest_start[current]
        ]
        if not tight:
            break
        current = min(tight)
        path.append(current)
    return tuple(reversed(path))
