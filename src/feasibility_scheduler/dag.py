"""DAG 构建、环检测、确定性拓扑排序与直接后继查询。

对应规格第 3.1 节（任务 DAG）与第 2.2 节 dag 模块职责。

确定性约定
----------
拓扑序只由"节点集合 + 依赖边集合"决定，与 `tasks` 的传入顺序、
`Task.dependencies` 元组内的顺序无关：每轮从当前入度为 0 的节点中
取 ID 字典序最小者（最小堆），因此同一张图任何输入顺序都得到同一结果。

边方向约定沿用 `models.Task.dependencies`：`v` 依赖 `u` 记为边 `(u, v)`，
`v` 位于 `u` 之后。`successors_of(u)` 只返回直接后继，不做传递闭包。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .models import DAG, Task


class DagError(ValueError):
    """DAG 构建或查询失败的基类。"""


class DuplicateTaskIdError(DagError):
    """同一张图内出现重复任务 ID。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"重复任务 ID: {task_id}")


class MissingDependencyError(DagError):
    """依赖引用了图中不存在的任务 ID。"""

    def __init__(self, task_id: str, dependency: str) -> None:
        self.task_id = task_id
        self.dependency = dependency
        super().__init__(f"缺失依赖: {task_id} -> {dependency}")


class CycleError(DagError):
    """图中存在环形依赖。"""

    def __init__(self, cycle: Iterable[str]) -> None:
        self.cycle = tuple(cycle)
        super().__init__("检测到环形依赖: " + " -> ".join(self.cycle))


@dataclass(frozen=True)
class TaskGraph:
    """不可变任务图。

    节点按任务 ID 升序存放；后继 / 前驱映射同样按 ID 升序排列，
    保证同一张图在任何输入顺序下产生相同的对象内容。
    """

    tasks: tuple[Task, ...]
    successors: Mapping[str, tuple[str, ...]]
    predecessors: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "successors", MappingProxyType(dict(self.successors)))
        object.__setattr__(self, "predecessors", MappingProxyType(dict(self.predecessors)))

    # ------------------------------------------------------------------ 构建

    @classmethod
    def build(cls, source: Iterable[Task] | DAG) -> "TaskGraph":
        """从任务序列或 `DAG` 构建图；重复 ID、缺失依赖、环一律拒绝。"""
        tasks = _as_task_tuple(source)
        _reject_duplicates(tasks)
        _reject_missing_dependencies(tasks)
        cycle = _find_cycle(tasks)
        if cycle is not None:
            raise CycleError(cycle)
        ordered = tuple(sorted(tasks, key=lambda task: task.id))
        successors: dict[str, list[str]] = {task.id: [] for task in ordered}
        predecessors: dict[str, list[str]] = {task.id: [] for task in ordered}
        for task in ordered:
            for dependency in sorted(set(task.dependencies)):
                successors[dependency].append(task.id)
                predecessors[task.id].append(dependency)
        return cls(
            tasks=ordered,
            successors={key: tuple(sorted(value)) for key, value in successors.items()},
            predecessors={key: tuple(sorted(value)) for key, value in predecessors.items()},
        )

    # ------------------------------------------------------------------ 查询

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(task.id for task in self.tasks)

    def __len__(self) -> int:
        return len(self.tasks)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self.successors

    def __iter__(self):
        return iter(self.tasks)

    def task(self, task_id: str) -> Task:
        """按 ID 取任务；未知 ID 抛 `KeyError`。"""
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)

    def successors_of(self, task_id: str) -> tuple[str, ...]:
        """返回直接后继（依赖该任务的任务），按 ID 升序。"""
        try:
            return self.successors[task_id]
        except KeyError:
            raise KeyError(task_id) from None

    def predecessors_of(self, task_id: str) -> tuple[str, ...]:
        """返回直接依赖（该任务依赖的任务），按 ID 升序。"""
        try:
            return self.predecessors[task_id]
        except KeyError:
            raise KeyError(task_id) from None

    @property
    def edges(self) -> tuple[tuple[str, str], ...]:
        """依赖边 `(u, v)`：`v` 依赖 `u`，按 `(u, v)` 升序。"""
        return tuple(
            (dependency, task_id)
            for task_id in self.task_ids
            for dependency in self.predecessors[task_id]
        )

    def roots(self) -> tuple[str, ...]:
        """无依赖、可直接启动的任务 ID。"""
        return tuple(task_id for task_id in self.task_ids if not self.predecessors[task_id])

    def leaves(self) -> tuple[str, ...]:
        """无后继的任务 ID。"""
        return tuple(task_id for task_id in self.task_ids if not self.successors[task_id])

    # -------------------------------------------------------------- 拓扑排序

    def topological_order(self) -> tuple[str, ...]:
        """Kahn 拓扑排序，每轮取 ID 最小的就绪节点。

        返回顺序满足：对任意依赖边 `(u, v)`，`u` 都在 `v` 之前。
        图在构建时已排除环，此处的剩余节点检查仅作防御。
        """
        indegree = {task_id: len(self.predecessors[task_id]) for task_id in self.task_ids}
        ready = [task_id for task_id in self.task_ids if indegree[task_id] == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            current = heapq.heappop(ready)
            order.append(current)
            for successor in self.successors[current]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    heapq.heappush(ready, successor)
        if len(order) != len(self.tasks):
            raise CycleError(find_cycle(self) or ())
        return tuple(order)


def build_dag(source: Iterable[Task] | DAG) -> TaskGraph:
    """`TaskGraph.build` 的函数式入口。"""
    return TaskGraph.build(source)


def find_cycle(source: Iterable[Task] | DAG) -> tuple[str, ...] | None:
    """返回一条闭合环路径（首尾同 ID），无环时返回 `None`。

    遍历顺序固定（起点与依赖均按 ID 升序），因此同一张图总是报告同一条环。
    """
    return _find_cycle(_as_task_tuple(source))


# ---------------------------------------------------------------------- 内部


def _as_task_tuple(source: Iterable[Task] | DAG) -> tuple[Task, ...]:
    tasks = source.tasks if isinstance(source, DAG) else source
    return tuple(tasks)


def _reject_duplicates(tasks: tuple[Task, ...]) -> None:
    seen: set[str] = set()
    for task_id in sorted(task.id for task in tasks):
        if task_id in seen:
            raise DuplicateTaskIdError(task_id)
        seen.add(task_id)


def _reject_missing_dependencies(tasks: tuple[Task, ...]) -> None:
    known = {task.id for task in tasks}
    for task in sorted(tasks, key=lambda item: item.id):
        for dependency in sorted(task.dependencies):
            if dependency not in known:
                raise MissingDependencyError(task.id, dependency)


def _find_cycle(tasks: tuple[Task, ...]) -> tuple[str, ...] | None:
    adjacency = {task.id: tuple(sorted(set(task.dependencies))) for task in tasks}
    state: dict[str, int] = {}  # 0/缺失=未访问，1=在栈上，2=已完成

    for start in sorted(adjacency):
        if state.get(start, 0):
            continue
        state[start] = 1
        path = [start]
        stack = [(start, iter(adjacency[start]))]
        while stack:
            node, neighbours = stack[-1]
            neighbour = next(neighbours, None)
            if neighbour is None:
                stack.pop()
                path.pop()
                state[node] = 2
                continue
            if neighbour not in adjacency:  # 外部依赖，由缺失依赖检查负责
                continue
            status = state.get(neighbour, 0)
            if status == 1:
                return tuple(path[path.index(neighbour):] + [neighbour])
            if status == 0:
                state[neighbour] = 1
                path.append(neighbour)
                stack.append((neighbour, iter(adjacency[neighbour])))
    return None
