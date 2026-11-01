"""独立验收与总运行成功判定（规格第 5 节）。

职责边界
--------
1. **任务级验收**：验收程序（``Acceptor``）只看任务定义与 provider 响应，
   给出 :class:`AcceptanceReport`。默认实现 :func:`contract_acceptor` 按任务的
   输出契约做确定性检查；API 成功、响应非空都不等于验收通过。
2. **验收结果归一**：:func:`run_acceptance` 把三种异常情况区分开：
   验收程序抛异常 → 带 ``error`` 的报告；返回 ``None`` → 报告缺失；返回值不是
   本任务的报告 → 视为验收程序异常。三者都不能被当成通过。
3. **总运行判定**：:func:`judge_run` 只在以下条件同时成立时给出 ``ok``：
   任务图合法、全部必要任务在状态存储中为 ``ACCEPTED``（SchedulerCompleted）、
   每个任务都有通过且无异常的验收报告（IndependentAcceptancePass）、事件日志
   重放结果与状态存储逐任务一致（TraceComplete），以及调用方提供的外部门禁
   （如 SpecTestsPass、IntegrationPass）全部为真。scheduler 的“完成”不能覆盖
   验收失败；``ok`` 为假时退出码固定为 1。
"""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .events import Event
from .fake_client import ApiResponse
from .models import Task
from .replay import ReplayError, replay
from .statemachine import TaskState
from .store import TaskNotFoundError, TaskStore

EXIT_OK = 0
EXIT_FAILED = 1


@dataclass(frozen=True)
class AcceptanceReport:
    """一次独立验收的结果。

    ``error`` 非空表示验收程序本身异常，此时结论不可用，``passed`` 必须为假。
    """

    task_id: str
    passed: bool
    checks: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id 不能为空")
        if not isinstance(self.passed, bool):
            raise ValueError("passed 必须是布尔值")
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "errors", tuple(self.errors))
        if self.passed and (self.errors or self.error):
            raise ValueError("带有错误的验收报告不能标记为通过")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "passed": self.passed,
            "checks": list(self.checks),
            "errors": list(self.errors),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AcceptanceReport":
        return cls(
            task_id=data["task_id"],
            passed=data["passed"],
            checks=tuple(data.get("checks", ())),
            errors=tuple(data.get("errors", ())),
            error=data.get("error"),
        )


#: 验收程序：输入任务与响应，返回报告；返回 ``None`` 表示没有产出报告。
Acceptor = Callable[[Task, ApiResponse], "AcceptanceReport | None"]

_JSON_TYPES: Mapping[str, Callable[[Any], bool]] = MappingProxyType(
    {
        "object": lambda value: isinstance(value, Mapping),
        "array": lambda value: isinstance(value, (list, tuple)),
        "string": lambda value: isinstance(value, str),
        "boolean": lambda value: isinstance(value, bool),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, Real) and not isinstance(value, bool),
        "null": lambda value: value is None,
    }
)
_CONTRACT_KEYS = frozenset({"type", "required", "properties"})


class ContractError(ValueError):
    """输出契约本身无法解释；验收程序无法给出结论。"""


def contract_acceptor(task: Task, response: ApiResponse) -> AcceptanceReport:
    """按输出契约确定性验收。

    支持的契约字段：``type``（顶层 JSON 类型）、``required``（必需键）、
    ``properties``（键 → ``{"type": ...}``）。出现其它字段说明契约超出本验收
    程序的能力，抛 :class:`ContractError`，由 :func:`run_acceptance` 记为验收
    程序异常，不按通过处理。
    """
    contract = task.output_contract
    if contract is None:
        raise ContractError(f"任务 {task.id!r} 没有输出契约，无法验收")
    unknown = sorted(set(contract) - _CONTRACT_KEYS)
    if unknown:
        raise ContractError("输出契约包含无法解释的字段：" + "、".join(unknown))

    checks: list[str] = ["response_ok"]
    errors: list[str] = []
    if response.task_id != task.id:
        errors.append(f"响应属于任务 {response.task_id!r}，不是 {task.id!r}")
    if not response.ok:
        errors.append(f"响应结果为 {response.outcome.value}，不是成功响应")
    body = response.parsed_body
    checks.append("body_parsed")
    if body is None:
        errors.append("响应正文无法解析为 JSON 对象")
        return AcceptanceReport(task.id, False, tuple(checks), tuple(errors))

    expected_type = contract.get("type")
    if expected_type is not None:
        checks.append(f"type:{expected_type}")
        if not _type_matches(expected_type, body):
            errors.append(f"响应正文类型不是 {expected_type}")

    for key in _as_names(contract.get("required", ()), "required"):
        checks.append(f"required:{key}")
        if key not in body:
            errors.append(f"缺少必需字段 {key!r}")

    properties = contract.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ContractError("properties 必须是映射")
    for key in sorted(properties):
        spec = properties[key]
        if not isinstance(spec, Mapping) or "type" not in spec:
            raise ContractError(f"properties[{key!r}] 缺少 type")
        if key not in body:
            continue
        checks.append(f"property:{key}:{spec['type']}")
        if not _type_matches(spec["type"], body[key]):
            errors.append(f"字段 {key!r} 的类型不是 {spec['type']}")

    return AcceptanceReport(task.id, not errors, tuple(checks), tuple(errors))


def run_acceptance(
    acceptor: Acceptor, task: Task, response: ApiResponse
) -> AcceptanceReport | None:
    """调用验收程序并归一结果；``None`` 表示验收报告缺失。"""
    try:
        report = acceptor(task, response)
    except Exception as exc:  # 验收程序异常必须显式记录，不能吞成通过
        return AcceptanceReport(
            task.id, False, error=f"验收程序异常：{type(exc).__name__}: {exc}"
        )
    if report is None:
        return None
    if not isinstance(report, AcceptanceReport) or report.task_id != task.id:
        return AcceptanceReport(
            task.id, False, error=f"验收程序返回了不合法的报告：{report!r}"
        )
    return report


@dataclass(frozen=True)
class RunVerdict:
    """总运行判定。``ok`` 为假时进程必须以非零码退出。"""

    ok: bool
    scheduler_completed: bool
    acceptance_passed: bool
    trace_complete: bool
    reasons: tuple[str, ...]
    external_gates: Mapping[str, bool | None] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(
            self, "external_gates", MappingProxyType(dict(self.external_gates))
        )

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_FAILED

    @classmethod
    def failure(cls, reason: str) -> "RunVerdict":
        return cls(
            ok=False,
            scheduler_completed=False,
            acceptance_passed=False,
            trace_complete=False,
            reasons=(reason,),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scheduler_completed": self.scheduler_completed,
            "acceptance_passed": self.acceptance_passed,
            "trace_complete": self.trace_complete,
            "external_gates": dict(self.external_gates),
            "reasons": list(self.reasons),
            "exit_code": self.exit_code,
        }


def check_trace(store: TaskStore, log_events: Iterable[Event]) -> tuple[str, ...]:
    """事件日志重放与状态存储的一致性问题；空元组表示一致。"""
    events = tuple(log_events)
    problems: list[str] = []
    if not any(event.type == "run_created" for event in events):
        problems.append("事件日志缺少 run_created")
    try:
        replayed = replay(events)
    except ReplayError as exc:
        problems.append(f"事件日志无法重放：{exc}")
        return tuple(problems)

    stored = {record.task_id: record.state for record in store.list_tasks()}
    replayed_states = {task_id: item.state for task_id, item in replayed.tasks.items()}
    for task_id in sorted(set(stored) | set(replayed_states)):
        left = stored.get(task_id)
        right = replayed_states.get(task_id)
        if left is not right:
            problems.append(
                f"任务 {task_id!r} 状态不一致：存储为 {_name(left)}，日志重放为 {_name(right)}"
            )

    store_transitions: dict[str, list[tuple[TaskState | None, TaskState]]] = {}
    for event in store.events():
        store_transitions.setdefault(event.task_id, []).append(
            (event.from_state, event.to_state)
        )
    for task_id, item in replayed.tasks.items():
        if tuple(store_transitions.get(task_id, ())) != item.transitions:
            problems.append(f"任务 {task_id!r} 的迁移序列与状态存储记录不一致")
    return tuple(problems)


def judge_run(
    *,
    task_ids: Iterable[str],
    store: TaskStore,
    log_events: Iterable[Event],
    reports: Mapping[str, AcceptanceReport],
    graph_errors: Iterable[str] = (),
    external_gates: Mapping[str, bool | None] | None = None,
) -> RunVerdict:
    """按规格第 5 节判定总运行是否成功。"""
    required = tuple(task_ids)
    graph_problems = tuple(graph_errors)
    reasons: list[str] = [f"任务图非法：{item}" for item in graph_problems]
    if not required and not graph_problems:
        reasons.append("没有任何必要任务，无法判定运行成功")

    scheduler_completed = bool(required) and not graph_problems
    acceptance_passed = bool(required) and not graph_problems
    for task_id in required:
        try:
            state = store.get_task(task_id).state
        except TaskNotFoundError:
            scheduler_completed = False
            reasons.append(f"状态存储中没有任务 {task_id!r}")
            state = None
        if state is not TaskState.ACCEPTED:
            scheduler_completed = False
            if state is not None:
                reasons.append(f"任务 {task_id!r} 未通过验收（当前状态 {state.value}）")

        report = reports.get(task_id)
        if report is None:
            acceptance_passed = False
            reasons.append(f"任务 {task_id!r} 缺少验收报告")
        elif report.error:
            acceptance_passed = False
            reasons.append(f"任务 {task_id!r} 验收程序异常：{report.error}")
        elif not report.passed:
            acceptance_passed = False
            reasons.append(
                f"任务 {task_id!r} 验收失败：" + ("；".join(report.errors) or "未说明原因")
            )

    trace_problems = check_trace(store, log_events)
    reasons.extend(trace_problems)

    gates = dict(external_gates or {})
    for name in sorted(gates):
        if gates[name] is not True:
            reasons.append(f"外部门禁 {name} 未通过或未评估（{gates[name]!r}）")

    return RunVerdict(
        ok=not reasons,
        scheduler_completed=scheduler_completed,
        acceptance_passed=acceptance_passed,
        trace_complete=not trace_problems,
        reasons=tuple(reasons),
        external_gates=gates,
    )


# ---------------------------------------------------------------------- 内部


def _type_matches(expected: Any, value: Any) -> bool:
    try:
        checker = _JSON_TYPES[expected]
    except (KeyError, TypeError):
        raise ContractError(f"无法解释的 JSON 类型：{expected!r}") from None
    return checker(value)


def _as_names(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ContractError(f"{field_name} 必须是字符串列表")
    if any(not isinstance(item, str) for item in value):
        raise ContractError(f"{field_name} 必须是字符串列表")
    return tuple(value)


def _name(state: TaskState | None) -> str:
    return state.value if state is not None else "（无）"
