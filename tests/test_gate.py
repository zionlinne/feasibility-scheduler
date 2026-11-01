"""gate.py 的派发门禁测试：Φ 与 LS<ES 合并为 READY / BLOCKED / DEFERRED。"""

from feasibility_scheduler.dag import build_dag
from feasibility_scheduler.gate import Decision, gate_graph, gate_task
from feasibility_scheduler.models import Constraint, Task
from feasibility_scheduler.phi import ConstraintCheck, Truth, Verdict, evaluate
from feasibility_scheduler.timing import analyze

DIAMOND = {"S": (), "L": ("S",), "R": ("S",), "J": ("L", "R")}
DURATIONS = {"S": 2, "L": 3, "R": 5, "J": 1}


def make_task(task_id, deps=(), constraints=(), constraints_declared=True):
    return Task(
        id=task_id,
        dependencies=tuple(deps),
        constraints=tuple(constraints),
        constraints_declared=constraints_declared,
        output_contract={"type": "object"},
        payload={"work": True},
    )


def diamond_tasks():
    return [make_task(task_id, deps) for task_id, deps in DIAMOND.items()]


def phi_all_true(tasks):
    return {task.id: evaluate(task) for task in tasks}


# ------------------------------------------------------------ 图级判定


def test_all_true_and_feasible_is_ready():
    tasks = diamond_tasks()
    timing = analyze(build_dag(tasks), DURATIONS, {"J": 10})
    result = gate_graph(phi_all_true(tasks), timing)

    assert result.decision is Decision.READY
    assert result.phi is Truth.TRUE
    assert all(item.dispatchable for item in result.tasks.values())


def test_missed_deadline_turns_phi_false_and_blocks():
    # J deadline=7，但 J 最早 7 开始、8 完成：J LS=6 < ES=7
    tasks = diamond_tasks()
    timing = analyze(build_dag(tasks), DURATIONS, {"J": 7})
    result = gate_graph(phi_all_true(tasks), timing)

    assert result.decision is Decision.BLOCKED
    assert result.phi is Truth.FALSE
    assert result["J"].phi is Truth.FALSE
    assert result["J"].decision is Decision.BLOCKED
    assert any("latest_start=6 < earliest_start=7" in r for r in result["J"].reasons)
    # 关键路径上游 S、R 同样 LS<ES；非关键的 L 仍有余量
    assert result["S"].decision is Decision.BLOCKED
    assert result["R"].decision is Decision.BLOCKED
    assert result["L"].decision is Decision.READY


def test_graph_with_unknown_is_deferred():
    tasks = diamond_tasks()
    tasks[1] = make_task("L", ("S",), constraints=[Constraint("files<=3", 3)])
    timing = analyze(build_dag(tasks), DURATIONS, {"J": 10})
    reports = {task.id: evaluate(task) for task in tasks}  # L 无证据 → UNKNOWN

    result = gate_graph(reports, timing)

    assert reports["L"].truth is Truth.UNKNOWN
    assert result["L"].decision is Decision.DEFERRED
    assert result.decision is Decision.DEFERRED
    assert result.phi is Truth.UNKNOWN


def test_missing_phi_report_defers_instead_of_passing():
    tasks = diamond_tasks()
    timing = analyze(build_dag(tasks), DURATIONS, {"J": 10})
    reports = phi_all_true(tasks)
    del reports["R"]

    result = gate_graph(reports, timing)

    assert result["R"].phi is Truth.UNKNOWN
    assert result.decision is Decision.DEFERRED


def test_blocked_takes_precedence_over_deferred():
    tasks = diamond_tasks()
    tasks[1] = make_task("L", ("S",), constraints=[Constraint("files<=3", 3)])
    timing = analyze(build_dag(tasks), DURATIONS, {"J": 7})
    result = gate_graph({task.id: evaluate(task) for task in tasks}, timing)

    assert result["L"].decision is Decision.DEFERRED
    assert result.decision is Decision.BLOCKED


def test_phi_false_from_constraint_blocks_even_when_time_feasible():
    tasks = diamond_tasks()
    tasks[2] = make_task("R", ("S",), constraints=[Constraint("budget<=10", 10)])
    timing = analyze(build_dag(tasks), DURATIONS, {"J": 10})
    reports = phi_all_true(tasks)
    reports["R"] = evaluate(tasks[2], [ConstraintCheck("budget<=10", Verdict.VIOLATED)])

    result = gate_graph(reports, timing)

    assert timing.feasible
    assert result["R"].decision is Decision.BLOCKED
    assert result.decision is Decision.BLOCKED


def test_no_deadline_is_not_a_time_violation():
    tasks = diamond_tasks()
    timing = analyze(build_dag(tasks), DURATIONS)
    result = gate_graph(phi_all_true(tasks), timing)

    assert all(timing[t].latest_start is None for t in DIAMOND)
    assert result.decision is Decision.READY


# ------------------------------------------------------------ 单任务合并


def test_unknown_plus_time_violation_is_false_not_unknown():
    task = make_task("J", constraints_declared=False)  # Φ=UNKNOWN
    timing = analyze(build_dag([task]), {"J": 5}, {"J": 3})["J"]  # LS=-2 < ES=0

    result = gate_task(evaluate(task), timing)

    assert result.phi is Truth.FALSE
    assert result.decision is Decision.BLOCKED


def test_only_true_is_ready():
    timing = analyze(build_dag([make_task("T")]), {"T": 1}, {"T": 5})["T"]
    cases = {
        Truth.TRUE: evaluate(make_task("T")),
        Truth.UNKNOWN: evaluate(make_task("T", constraints_declared=False)),
        Truth.FALSE: evaluate(
            make_task("T", constraints=[Constraint("c", 1)]),
            [ConstraintCheck("c", Verdict.VIOLATED)],
        ),
    }
    decisions = {truth: gate_task(report, timing).decision for truth, report in cases.items()}

    assert decisions == {
        Truth.TRUE: Decision.READY,
        Truth.UNKNOWN: Decision.DEFERRED,
        Truth.FALSE: Decision.BLOCKED,
    }


# ------------------------------------------------------- 重算后门禁随之改变


def test_gate_follows_recomputed_timing_after_duration_change():
    tasks = diamond_tasks()
    graph = build_dag(tasks)
    reports = phi_all_true(tasks)

    assert gate_graph(reports, analyze(graph, DURATIONS, {"J": 9})).decision is Decision.READY
    # R 从 5 增至 7：J ES=9，EF=10 > deadline 9 → BLOCKED
    longer = dict(DURATIONS, R=7)
    assert gate_graph(reports, analyze(graph, longer, {"J": 9})).decision is Decision.BLOCKED


def test_gate_follows_recomputed_timing_after_dependency_change():
    tasks = diamond_tasks()
    reports = phi_all_true(tasks)
    assert gate_graph(reports, analyze(build_dag(tasks), DURATIONS, {"J": 9})).decision is Decision.READY

    # 新增 R → L：J 最早 10 开始，deadline 9 已不可达
    rewired = [make_task(t, ("S", "R") if t == "L" else d) for t, d in DIAMOND.items()]
    timing = analyze(build_dag(rewired), DURATIONS, {"J": 9})
    assert gate_graph(phi_all_true(rewired), timing).decision is Decision.BLOCKED


# ------------------------------------------------------------- 固定图文件


def fixture_reports(graph):
    return {task.id: evaluate(task, graph.checks.get(task.id, ())) for task in graph.tasks}


def test_unknown_fixture_is_deferred(load_graph):
    # parse 省略约束字段，store 已声明约束但没有证据；fetch 有满足证据
    graph = load_graph("03_unknown.json")
    timing = analyze(build_dag(graph.tasks), graph.durations, graph.deadlines)
    result = gate_graph(fixture_reports(graph), timing)

    assert timing.feasible
    assert result["fetch"].decision is Decision.READY
    assert result["parse"].phi is Truth.UNKNOWN
    assert result["store"].phi is Truth.UNKNOWN
    assert result["parse"].decision is Decision.DEFERRED
    assert result["store"].decision is Decision.DEFERRED
    assert result.decision is Decision.DEFERRED


def test_deadline_miss_fixture_is_blocked(load_graph):
    graph = load_graph("04_deadline_miss.json")
    timing = analyze(build_dag(graph.tasks), graph.durations, graph.deadlines)
    result = gate_graph(fixture_reports(graph), timing)

    assert result.decision is Decision.BLOCKED
    assert result.phi is Truth.FALSE
    assert all(item.decision is Decision.BLOCKED for item in result.tasks.values())
    assert any("latest_start=4 < earliest_start=6" in r for r in result["ship"].reasons)


def test_diamond_fixture_gate_ready_once_constraints_declared(load_graph):
    # 菱形图文件不带约束字段；显式声明“无额外约束”后时间可行 → READY
    graph = load_graph("02_diamond.json")
    tasks = [make_task(t.id, t.dependencies) for t in graph.tasks]
    timing = analyze(build_dag(tasks), graph.durations, graph.deadlines)

    assert gate_graph(phi_all_true(tasks), timing).decision is Decision.READY
