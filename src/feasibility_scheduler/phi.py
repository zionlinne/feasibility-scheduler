"""三值可行性门禁 Φ 的求值。

对应规格第 3.2 节：

- TRUE：所有已声明且必要的硬约束均已验证并满足；
- FALSE：至少一个必要硬约束已验证为违反；
- UNKNOWN：必要信息缺失、不可验证或证据不足。

实现约定
--------
1. 本模块不解释谓词语义。它只汇聚确定性检查给出的裁决
   （`ConstraintCheck`），把"没有裁决"记为 UNKNOWN，绝不补成满足。
2. 所有已声明的约束都视为必要硬约束（当前数据契约没有"非必要"标记）。
3. `Task.constraints_declared` 为假表示约束字段根本没有提供，
   即使 `constraints` 为空也判 UNKNOWN——空元组既可能是"确实没有额外约束"，
   也可能是"输入漏掉约束"，两者不得互相冒充。
4. 证据中出现未声明的约束名，说明输入/证据对不上号，判 UNKNOWN，不按满足处理。
5. 已声明的约束若同时收到冲突证据，取最严重者：VIOLATED > UNKNOWN > SATISFIED。
6. UNKNOWN 与 FALSE 都不派发；UNKNOWN 的后续动作固定为收集更多信息，
   不提供"默认放行"分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .models import Task


class Truth(str, Enum):
    """Φ 的三值结果。"""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class Verdict(str, Enum):
    """单条硬约束的确定性检查结果。"""

    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN = "UNKNOWN"


ACTION_DISPATCH = "dispatch"
ACTION_REJECT = "reject"
ACTION_REQUEST_INFO = "request-more-information"

#: UNKNOWN 的显式处理策略：补齐证据后重判，期间不得派发。
UNKNOWN_POLICY = ACTION_REQUEST_INFO

_SEVERITY = {Verdict.SATISFIED: 0, Verdict.UNKNOWN: 1, Verdict.VIOLATED: 2}


class FeasibilityError(RuntimeError):
    """在 Φ 不为 TRUE 时尝试派发任务。"""

    def __init__(self, report: "PhiReport") -> None:
        self.report = report
        detail = "；".join(report.reasons) or "无补充说明"
        super().__init__(
            f"Φ={report.truth.value}，禁止派发；处理策略={report.next_action}；原因：{detail}"
        )


@dataclass(frozen=True)
class ConstraintCheck:
    """一条由确定性代码给出的约束裁决及其证据来源。"""

    name: str
    verdict: "Verdict | str"
    source: str = "deterministic-check"


@dataclass(frozen=True)
class ConstraintFinding:
    """Φ 对单条已声明约束的记录（谓词、结果、证据来源）。"""

    name: str
    verdict: Verdict
    evidence: str


@dataclass(frozen=True)
class PhiReport:
    """一次 Φ 求值的完整记录。"""

    truth: Truth
    findings: tuple[ConstraintFinding, ...] = ()
    reasons: tuple[str, ...] = ()
    next_action: str = ACTION_REJECT

    @property
    def dispatchable(self) -> bool:
        """只有 TRUE 允许派发；UNKNOWN 不默认放行。"""
        return self.truth is Truth.TRUE

    def describe(self) -> str:
        """供事件日志使用的一行摘要。"""
        findings = "、".join(
            f"{item.name}={item.verdict.value}" for item in self.findings
        ) or "无已声明约束"
        return f"Φ={self.truth.value}；{findings}；策略={self.next_action}"


def evaluate(
    task: Task,
    checks: Iterable[ConstraintCheck] = (),
) -> PhiReport:
    """求值任务的 Φ。

    `checks` 是确定性验证程序产出的裁决集合；缺失的裁决按 UNKNOWN 计入。
    """
    declared_names = tuple(sorted({constraint.name for constraint in task.constraints}))
    collected, conflict_notes = _collect_checks(checks)
    reasons: list[str] = list(conflict_notes)

    findings = tuple(
        ConstraintFinding(
            name=name,
            verdict=(collected[name].verdict if name in collected else Verdict.UNKNOWN),
            evidence=(
                collected[name].source
                if name in collected
                else "没有可验证的证据"
            ),
        )
        for name in declared_names
    )

    undeclared = tuple(sorted(set(collected) - set(declared_names)))
    if undeclared:
        reasons.append(
            "证据引用了未声明的约束，输入与证据不一致: " + "、".join(undeclared)
        )
        return PhiReport(Truth.UNKNOWN, findings, tuple(reasons), ACTION_REQUEST_INFO)

    violated = tuple(item.name for item in findings if item.verdict is Verdict.VIOLATED)
    if violated:
        reasons.append("必要硬约束已验证为违反: " + "、".join(violated))
        return PhiReport(Truth.FALSE, findings, tuple(reasons), ACTION_REJECT)

    if not task.constraints_declared:
        reasons.append(
            "约束字段缺省（constraints_declared=False），"
            "无法区分“没有额外约束”与“输入漏掉约束”"
        )
        return PhiReport(Truth.UNKNOWN, findings, tuple(reasons), ACTION_REQUEST_INFO)

    unresolved = tuple(item.name for item in findings if item.verdict is Verdict.UNKNOWN)
    if unresolved:
        reasons.append("必要信息缺失或不可验证: " + "、".join(unresolved))
        return PhiReport(Truth.UNKNOWN, findings, tuple(reasons), ACTION_REQUEST_INFO)

    if not declared_names:
        reasons.append("已显式声明没有额外硬约束（constraints_declared=True 且集合为空）")
    else:
        reasons.append(f"{len(declared_names)} 条已声明硬约束均已由确定性证据验证为满足")
    return PhiReport(Truth.TRUE, findings, tuple(reasons), ACTION_DISPATCH)


def require_dispatchable(
    task: Task,
    checks: Iterable[ConstraintCheck] = (),
) -> PhiReport:
    """派发前的唯一总门禁：Φ 不为 TRUE 时抛 `FeasibilityError`。"""
    report = evaluate(task, checks)
    if not report.dispatchable:
        raise FeasibilityError(report)
    return report


# ---------------------------------------------------------------------- 内部


def _normalize_verdict(raw: "Verdict | str") -> Verdict:
    if isinstance(raw, Verdict):
        return raw
    if isinstance(raw, str):
        try:
            return Verdict(raw.strip().upper())
        except ValueError:
            return Verdict.UNKNOWN
    return Verdict.UNKNOWN


def _collect_checks(
    checks: Iterable[ConstraintCheck],
) -> tuple[dict[str, ConstraintCheck], tuple[str, ...]]:
    """按约束名合并裁决；同名冲突取最严重者并记录。"""
    collected: dict[str, ConstraintCheck] = {}
    notes: list[str] = []
    for check in checks:
        verdict = _normalize_verdict(check.verdict)
        existing = collected.get(check.name)
        if existing is None:
            collected[check.name] = ConstraintCheck(check.name, verdict, check.source)
            continue
        if existing.verdict is not verdict:
            notes.append(
                f"约束 {check.name} 收到冲突证据（{existing.verdict.value} 与 {verdict.value}），取更严重者"
            )
        if _SEVERITY[verdict] > _SEVERITY[existing.verdict]:
            collected[check.name] = ConstraintCheck(check.name, verdict, check.source)
    return collected, tuple(notes)
