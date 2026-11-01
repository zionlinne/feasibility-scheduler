"""派发门禁：把 Φ 结果与时间可行性（LS < ES）合并为 READY / BLOCKED / DEFERRED。

对应规格第 3.2、3.4 至 3.6 节。

合并规则
--------
1. 时间分析给出 `LS < ES`（在当前依赖、时长与 deadline 下已无可行启动安排）
   视为一条已验证违反的硬约束，合并后的 Φ 为 FALSE。
2. 合并后 Φ 的优先级：FALSE > UNKNOWN > TRUE。
   时间违反不会被 UNKNOWN 掩盖，UNKNOWN 也不会被时间可行“补成”TRUE。
3. 决策映射：FALSE → BLOCKED，UNKNOWN → DEFERRED，只有 TRUE → READY。
4. 图级决策：任一任务 BLOCKED 则图 BLOCKED；否则任一 DEFERRED 则图 DEFERRED；
   全部 READY 才 READY。缺少某任务的 Φ 报告按 UNKNOWN 处理。
5. 无 deadline（LS 为 None）不构成时间违反，也不伪造边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .phi import PhiReport, Truth
from .timing import TaskTiming, TimingReport


class Decision(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"


_DECISION = {
    Truth.TRUE: Decision.READY,
    Truth.FALSE: Decision.BLOCKED,
    Truth.UNKNOWN: Decision.DEFERRED,
}
_TRUTH_SEVERITY = {Truth.TRUE: 0, Truth.UNKNOWN: 1, Truth.FALSE: 2}
_DECISION_SEVERITY = {Decision.READY: 0, Decision.DEFERRED: 1, Decision.BLOCKED: 2}


@dataclass(frozen=True)
class GateResult:
    """单个任务的门禁结果。"""

    task_id: str
    phi: Truth
    decision: Decision
    reasons: tuple[str, ...]

    @property
    def dispatchable(self) -> bool:
        return self.decision is Decision.READY


@dataclass(frozen=True)
class GraphGateResult:
    """整张图的门禁结果。"""

    decision: Decision
    tasks: Mapping[str, GateResult]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", MappingProxyType(dict(self.tasks)))

    @property
    def phi(self) -> Truth:
        return {
            Decision.READY: Truth.TRUE,
            Decision.DEFERRED: Truth.UNKNOWN,
            Decision.BLOCKED: Truth.FALSE,
        }[self.decision]

    def __getitem__(self, task_id: str) -> GateResult:
        return self.tasks[task_id]


def gate_task(phi_report: PhiReport | None, timing: TaskTiming) -> GateResult:
    """合并单个任务的 Φ 与时间可行性。`phi_report` 为 None 视为 UNKNOWN。"""
    reasons: list[str] = []
    if phi_report is None:
        truth = Truth.UNKNOWN
        reasons.append("缺少 Φ 报告，按 UNKNOWN 处理")
    else:
        truth = phi_report.truth
        reasons.extend(phi_report.reasons)

    if timing.infeasible:
        reasons.append(
            f"时间不可行：latest_start={timing.latest_start} < "
            f"earliest_start={timing.earliest_start}"
        )
        truth = _worse(truth, Truth.FALSE)

    return GateResult(
        task_id=timing.task_id,
        phi=truth,
        decision=_DECISION[truth],
        reasons=tuple(reasons),
    )


def gate_graph(
    phi_reports: Mapping[str, PhiReport],
    timing_report: TimingReport,
) -> GraphGateResult:
    """对整张图逐任务合并，再取最严重的决策作为图级结果。"""
    results = {
        task_id: gate_task(phi_reports.get(task_id), timing_report[task_id])
        for task_id in timing_report.order
    }
    decision = max(
        (result.decision for result in results.values()),
        key=_DECISION_SEVERITY.__getitem__,
        default=Decision.READY,
    )
    return GraphGateResult(decision=decision, tasks=results)


def _worse(left: Truth, right: Truth) -> Truth:
    return left if _TRUTH_SEVERITY[left] >= _TRUTH_SEVERITY[right] else right
