"""dag.py 的确定性测试：构建、环检测、拓扑排序、直接后继查询。"""

import itertools
import json
from dataclasses import FrozenInstanceError

import pytest

from feasibility_scheduler.dag import (
    CycleError,
    DuplicateTaskIdError,
    MissingDependencyError,
    TaskGraph,
    build_dag,
    find_cycle,
)
from feasibility_scheduler.models import DAG, Task


def make_task(task_id, dependencies=()):
    return Task(
        id=task_id,
        dependencies=tuple(dependencies),
        output_contract={"type": "object"},
        payload={"work": True},
    )


def diamond():
    """a -> b、a -> c、b/c -> d，含多种合法拓扑序。"""
    return [
        make_task("a"),
        make_task("b", ["a"]),
        make_task("c", ["a"]),
        make_task("d", ["b", "c"]),
    ]


def load_fixture(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Task(
            id=item["id"],
            dependencies=tuple(item["dependencies"]),
            output_contract={"type": "object"},
        )
        for item in data["tasks"]
    ]


def test_topological_order_is_canonical_and_respects_edges():
    graph = build_dag(diamond())
    order = graph.topological_order()

    assert order == ("a", "b", "c", "d")
    position = {task_id: index for index, task_id in enumerate(order)}
    assert all(position[u] < position[v] for u, v in graph.edges)


def test_topological_order_ignores_input_order():
    tasks = diamond()
    expected = build_dag(tasks).topological_order()

    seen = set()
    for permutation in itertools.permutations(tasks):
        shuffled = [
            Task(
                id=task.id,
                dependencies=tuple(reversed(task.dependencies)),
                output_contract=task.output_contract,
                payload=task.payload,
            )
            for task in permutation
        ]
        order = build_dag(shuffled).topological_order()
        seen.add(order)

    assert seen == {expected}
    assert expected == ("a", "b", "c", "d")


def test_parallel_roots_are_ordered_by_id_regardless_of_input_order():
    tasks = [make_task("zeta"), make_task("alpha"), make_task("merge", ["zeta", "alpha"])]
    forward = build_dag(tasks).topological_order()
    backward = build_dag(list(reversed(tasks))).topological_order()

    assert forward == backward == ("alpha", "zeta", "merge")


def test_successors_are_direct_only():
    graph = build_dag([make_task("a"), make_task("b", ["a"]), make_task("c", ["b"])])

    assert graph.successors_of("a") == ("b",)
    assert graph.successors_of("c") == ()
    assert graph.predecessors_of("c") == ("b",)
    assert graph.predecessors_of("a") == ()
    assert graph.edges == (("a", "b"), ("b", "c"))


def test_successor_query_is_sorted_and_complete():
    graph = build_dag(
        [
            make_task("root"),
            make_task("c", ["root"]),
            make_task("a", ["root"]),
            make_task("b", ["root"]),
        ]
    )

    assert graph.successors_of("root") == ("a", "b", "c")
    assert graph.predecessors_of("c") == ("root",)
    assert graph.roots() == ("root",)
    assert graph.leaves() == ("a", "b", "c")


def test_unknown_task_id_raises_key_error():
    graph = build_dag(diamond())

    with pytest.raises(KeyError):
        graph.successors_of("nope")
    with pytest.raises(KeyError):
        graph.predecessors_of("nope")
    with pytest.raises(KeyError):
        graph.task("nope")


def test_cycle_is_rejected_with_reproducible_path():
    tasks = [make_task("a", ["c"]), make_task("b", ["a"]), make_task("c", ["b"])]

    with pytest.raises(CycleError) as excinfo:
        build_dag(tasks)

    assert excinfo.value.cycle == ("a", "c", "b", "a")
    assert excinfo.value.cycle[0] == excinfo.value.cycle[-1]
    # 输入顺序不影响报告的环
    with pytest.raises(CycleError) as reordered:
        build_dag(list(reversed(tasks)))
    assert reordered.value.cycle == excinfo.value.cycle


def test_cycle_in_fixed_fixture_graph_is_rejected(graph_fixture_dir):
    tasks = load_fixture(graph_fixture_dir / "05_cycle.json")

    assert find_cycle(tasks) is not None
    with pytest.raises(CycleError):
        build_dag(tasks)


def test_self_dependency_is_a_cycle():
    with pytest.raises(CycleError):
        build_dag([make_task("a", ["a"])])


def test_find_cycle_returns_none_when_acyclic():
    assert find_cycle(diamond()) is None
    assert find_cycle(DAG(tuple(diamond()))) is None


def test_duplicate_task_id_is_rejected():
    with pytest.raises(DuplicateTaskIdError) as excinfo:
        build_dag([make_task("x"), make_task("x")])

    assert excinfo.value.task_id == "x"


def test_missing_dependency_is_rejected():
    with pytest.raises(MissingDependencyError) as excinfo:
        build_dag([make_task("x", ["ghost"])])

    assert (excinfo.value.task_id, excinfo.value.dependency) == ("x", "ghost")


def test_empty_graph_has_empty_order():
    graph = build_dag([])

    assert graph.task_ids == ()
    assert graph.topological_order() == ()
    assert graph.roots() == ()


def test_single_node_graph():
    graph = build_dag([make_task("only")])

    assert graph.topological_order() == ("only",)
    assert graph.successors_of("only") == ()
    assert len(graph) == 1
    assert "only" in graph


def test_build_accepts_dag_object_and_is_immutable():
    graph = build_dag(DAG(tuple(diamond())))

    assert graph.topological_order() == ("a", "b", "c", "d")
    with pytest.raises(FrozenInstanceError):
        graph.tasks = ()
    with pytest.raises(TypeError):
        graph.successors["a"] = ()


def test_fixed_graphs_build_deterministically(graph_fixture_dir):
    for name in ("01_linear.json", "02_diamond.json", "03_unknown.json", "04_deadline_miss.json"):
        tasks = load_fixture(graph_fixture_dir / name)
        forward = build_dag(tasks)
        backward = build_dag(list(reversed(tasks)))

        assert forward.topological_order() == backward.topological_order()
        assert forward.edges == backward.edges
        assert forward.successors == backward.successors


def test_diamond_fixture_successors(graph_fixture_dir):
    graph = build_dag(load_fixture(graph_fixture_dir / "02_diamond.json"))

    assert graph.topological_order() == ("S", "L", "R", "J")
    assert graph.successors_of("S") == ("L", "R")
    assert graph.successors_of("L") == ("J",)
    assert graph.successors_of("R") == ("J",)
    assert graph.predecessors_of("J") == ("L", "R")
    assert graph.successors_of("J") == ()


def test_duplicate_dependency_entry_is_a_single_edge():
    graph = build_dag([make_task("a"), make_task("b", ["a", "a"])])

    assert graph.predecessors_of("b") == ("a",)
    assert graph.successors_of("a") == ("b",)
    assert graph.edges == (("a", "b"),)
    assert graph.topological_order() == ("a", "b")


def test_acyclic_graph_reports_no_cycle():
    graph = build_dag(diamond())
    assert find_cycle(graph) is None
    assert graph.roots() == ("a",)
    assert graph.leaves() == ("d",)


def test_task_lookup_and_iteration():
    graph = build_dag(diamond())

    assert graph.task("b").dependencies == ("a",)
    assert [task.id for task in graph] == ["a", "b", "c", "d"]
    assert graph.task_ids == ("a", "b", "c", "d")


def test_graph_build_is_repeatable():
    first = build_dag(diamond())
    second = build_dag(diamond())

    assert first.tasks == second.tasks
    assert first.successors == second.successors
    assert first.topological_order() == second.topological_order()
    assert isinstance(first, TaskGraph)
