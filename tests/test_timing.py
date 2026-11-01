"""timing.py 的时间分析测试：期望值均为手算。"""

import pytest

from feasibility_scheduler.dag import build_dag
from feasibility_scheduler.models import Task
from feasibility_scheduler.timing import (
    SOURCE_ACTUAL,
    SOURCE_ESTIMATED,
    Duration,
    MissingDurationError,
    TimingError,
    analyze,
)


def make_graph(edges):
    """edges: {task_id: (依赖, ...)}"""
    return build_dag([Task(id=task_id, dependencies=tuple(deps)) for task_id, deps in edges.items()])


LINEAR = {"A": (), "B": ("A",), "C": ("B",)}
DIAMOND = {"S": (), "L": ("S",), "R": ("S",), "J": ("L", "R")}


def as_rows(report):
    return {
        task_id: (
            t.earliest_start,
            t.earliest_finish,
            t.latest_start,
            t.latest_finish,
            t.total_slack,
        )
        for task_id, t in report.timings.items()
    }


# ------------------------------------------------------------- 手算期望值


def test_linear_chain_with_deadline():
    # A=2, B=3, C=4；C 的 deadline=12
    # 正向：A[0,2] B[2,5] C[5,9]
    # 反向：C LF=12 LS=8；B LF=8 LS=5；A LF=5 LS=3；slack 均为 3
    report = analyze(make_graph(LINEAR), {"A": 2, "B": 3, "C": 4}, {"C": 12})

    assert as_rows(report) == {
        "A": (0, 2, 3, 5, 3),
        "B": (2, 5, 5, 8, 3),
        "C": (5, 9, 8, 12, 3),
    }
    assert report.critical_path == ("A", "B", "C")
    assert report.makespan == 9
    assert report.feasible


def test_diamond_with_deadline_on_join():
    # S=2, L=3, R=5, J=1；J 的 deadline=10
    # 正向：S[0,2] L[2,5] R[2,7] J[7,8]
    # 反向：J LF=10 LS=9；L LF=9 LS=6；R LF=9 LS=4；S LF=min(6,4)=4 LS=2
    # slack：S=2 L=4 R=2 J=2
    report = analyze(make_graph(DIAMOND), {"S": 2, "L": 3, "R": 5, "J": 1}, {"J": 10})

    assert as_rows(report) == {
        "S": (0, 2, 2, 4, 2),
        "L": (2, 5, 6, 9, 4),
        "R": (2, 7, 4, 9, 2),
        "J": (7, 8, 9, 10, 2),
    }
    assert report.critical_path == ("S", "R", "J")
    assert report.makespan == 8


def test_diamond_deadline_on_branch_and_own_deadline_tighter_than_successor():
    # 同一菱形，J deadline=10，L 自身 deadline=4（比后继给出的 9 更紧）
    # L LF=min(4, 9)=4 LS=1 → slack=1-2=-1（不可行）
    # S LF=min(L.LS=1, R.LS=4)=1 LS=-1
    report = analyze(
        make_graph(DIAMOND), {"S": 2, "L": 3, "R": 5, "J": 1}, {"J": 10, "L": 4}
    )

    assert report["L"].latest_finish == 4
    assert report["L"].latest_start == 1
    assert report["L"].total_slack == -1
    assert report["S"].latest_start == -1
    assert report["S"].total_slack == -1
    assert report["R"].total_slack == 2
    assert report.infeasible_tasks == ("S", "L")  # 按拓扑序
    assert not report.feasible


def test_input_order_does_not_change_result():
    tasks = [Task(id=k, dependencies=v) for k, v in DIAMOND.items()]
    durations = {"S": 2, "L": 3, "R": 5, "J": 1}
    forward = analyze(tasks, durations, {"J": 10})
    backward = analyze(list(reversed(tasks)), durations, {"J": 10})
    assert as_rows(forward) == as_rows(backward)
    assert forward.critical_path == backward.critical_path


# ------------------------------------------------------- 无 deadline → None


def test_no_deadline_gives_none_not_infinity_or_zero():
    report = analyze(make_graph(LINEAR), {"A": 2, "B": 3, "C": 4})

    for timing in report.timings.values():
        assert timing.latest_start is None
        assert timing.latest_finish is None
        assert timing.total_slack is None
        assert timing.remaining_slack is None
        assert timing.start_window is None
        assert not timing.infeasible
    # 正向结果照常
    assert report["C"].earliest_finish == 9
    assert report.critical_path == ("A", "B", "C")


def test_unconstrained_upstream_stays_none_while_constrained_branch_is_bounded():
    # X → Y，X → Z；只有 Z 有 deadline。
    # Y 无 deadline 且无受约束的后继 → None；X 受 Z 约束 → 有限值。
    graph = make_graph({"X": (), "Y": ("X",), "Z": ("X",), "P": (), "Q": ("P",)})
    report = analyze(graph, {"X": 1, "Y": 2, "Z": 3, "P": 1, "Q": 1}, {"Z": 10})

    assert report["Z"].latest_start == 7
    assert report["X"].latest_start == 6
    assert report["Y"].latest_start is None
    # P → Q 整条链都没有 deadline
    assert report["Q"].latest_start is None
    assert report["P"].latest_start is None
    assert report["P"].total_slack is None


# ----------------------------------------------------------- slack 与窗口


def test_remaining_slack_is_relative_to_now_and_distinct_from_total_slack():
    report = analyze(make_graph(LINEAR), {"A": 2, "B": 3, "C": 4}, {"C": 12}, now=1)

    assert report["A"].total_slack == 3          # LS - ES = 3 - 0
    assert report["A"].remaining_slack == 2      # LS - now = 3 - 1
    assert report["C"].remaining_slack == 7      # 8 - 1
    assert report["B"].start_window == (2, 5)


def test_release_shifts_forward_pass():
    report = analyze(make_graph(LINEAR), {"A": 2, "B": 3, "C": 4}, {"C": 12}, release=4)

    assert report["A"].earliest_start == 4
    assert report["C"].earliest_finish == 13
    assert report["C"].latest_start == 8
    assert report["C"].total_slack == -1
    assert report.infeasible_tasks == ("A", "B", "C")


def test_estimated_and_actual_durations_are_distinguished():
    durations = {
        "A": Duration(estimated=2, actual=4),
        "B": Duration(estimated=3),
        "C": 4,
    }
    report = analyze(make_graph(LINEAR), durations, {"C": 12})

    assert report["A"].duration == 4
    assert report["A"].duration_source == SOURCE_ACTUAL
    assert report["B"].duration_source == SOURCE_ESTIMATED
    assert report["C"].duration_source == SOURCE_ESTIMATED
    # 实测比估计长 2 → B、C 顺延 2，slack 从 3 变为 1
    assert report["C"].earliest_finish == 11
    assert report["A"].total_slack == 1


# ------------------------------------------------------- 依赖 / 时长改变后重算


def test_recompute_after_duration_change():
    graph = make_graph(DIAMOND)
    base = analyze(graph, {"S": 2, "L": 3, "R": 5, "J": 1}, {"J": 10})
    assert base.critical_path == ("S", "R", "J")

    # L 从 3 改为 6：L[2,8] → J[8,9]；L LS=9-6=3，S LF=min(3,4)=3 LS=1
    changed = analyze(graph, {"S": 2, "L": 6, "R": 5, "J": 1}, {"J": 10})
    assert as_rows(changed) == {
        "S": (0, 2, 1, 3, 1),
        "L": (2, 8, 3, 9, 1),
        "R": (2, 7, 4, 9, 2),
        "J": (8, 9, 9, 10, 1),
    }
    assert changed.critical_path == ("S", "L", "J")
    # 原报告不受影响
    assert base["J"].earliest_start == 7


def test_recompute_after_dependency_change():
    durations = {"S": 2, "L": 3, "R": 5, "J": 1}
    base = analyze(make_graph(DIAMOND), durations, {"J": 10})
    assert base["L"].earliest_start == 2

    # 新增依赖 R → L：L 必须等 R 完成
    # 正向：S[0,2] R[2,7] L[7,10] J[10,11]
    # 反向：J LS=9；L LF=9 LS=6；R LF=min(LS(J)=9, LS(L)=6)=6 LS=1；S LF=min(6,1)=1 LS=-1
    rewired = dict(DIAMOND, L=("S", "R"))
    report = analyze(make_graph(rewired), durations, {"J": 10})
    assert as_rows(report) == {
        "S": (0, 2, -1, 1, -1),
        "R": (2, 7, 1, 6, -1),
        "L": (7, 10, 6, 9, -1),
        "J": (10, 11, 9, 10, -1),
    }
    assert report.critical_path == ("S", "R", "L", "J")
    assert not report.feasible


def test_recompute_after_removing_dependency():
    # 线性链去掉 B → C 的依赖：C 可从 0 开始
    durations = {"A": 2, "B": 3, "C": 4}
    report = analyze(make_graph({"A": (), "B": ("A",), "C": ()}), durations, {"C": 12})
    assert report["C"].earliest_start == 0
    assert report["C"].total_slack == 8
    assert report["B"].latest_start is None
    assert report["A"].latest_start is None
    assert report.critical_path == ("A", "B")


# ------------------------------------------------------------- 非法输入


def test_missing_duration_is_rejected():
    with pytest.raises(MissingDurationError):
        analyze(make_graph(LINEAR), {"A": 2, "B": 3})
    with pytest.raises(MissingDurationError):
        analyze(make_graph(LINEAR), {"A": 2, "B": 3, "C": Duration()})


@pytest.mark.parametrize("bad", [-1, "3", True, float("nan")])
def test_invalid_duration_is_rejected(bad):
    with pytest.raises(TimingError):
        analyze(make_graph(LINEAR), {"A": bad, "B": 3, "C": 4})


def test_unknown_task_in_inputs_is_rejected():
    with pytest.raises(TimingError):
        analyze(make_graph(LINEAR), {"A": 2, "B": 3, "C": 4, "Z": 1})
    with pytest.raises(TimingError):
        analyze(make_graph(LINEAR), {"A": 2, "B": 3, "C": 4}, {"Z": 5})


# ------------------------------------------------------------- 固定图文件


def test_diamond_fixture_matches_hand_computed_rows(load_graph):
    graph = load_graph("02_diamond.json")
    report = analyze(graph.tasks, graph.durations, graph.deadlines)

    assert as_rows(report) == {
        "S": (0, 2, 2, 4, 2),
        "L": (2, 5, 6, 9, 4),
        "R": (2, 7, 4, 9, 2),
        "J": (7, 8, 9, 10, 2),
    }
    assert report.critical_path == ("S", "R", "J")
    assert report.feasible


def test_deadline_miss_fixture_has_ls_below_es(load_graph):
    # design=2, build=4, ship=3；ship deadline=7
    # 正向：design[0,2] build[2,6] ship[6,9]
    # 反向：ship LF=7 LS=4；build LF=4 LS=0；design LF=0 LS=-2；slack 均为 -2
    graph = load_graph("04_deadline_miss.json")
    report = analyze(graph.tasks, graph.durations, graph.deadlines)

    assert as_rows(report) == {
        "design": (0, 2, -2, 0, -2),
        "build": (2, 6, 0, 4, -2),
        "ship": (6, 9, 4, 7, -2),
    }
    assert report["ship"].latest_start < report["ship"].earliest_start
    assert report.infeasible_tasks == ("design", "build", "ship")
    assert report.makespan == 9
    assert not report.feasible
