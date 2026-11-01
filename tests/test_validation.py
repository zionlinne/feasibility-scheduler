import json
import pytest
from feasibility_scheduler.dag import build_dag
from feasibility_scheduler.gate import Decision, gate_task
from feasibility_scheduler.models import Constraint, DAG, Task
from feasibility_scheduler.phi import Truth, evaluate
from feasibility_scheduler.timing import analyze
from feasibility_scheduler.validation import validate_graph, validate_tasks

def task(task_id="task", **kwargs):
    return Task(id=task_id, output_contract={"type": "object"}, payload={"work": True}, **kwargs)

def test_default_constraints_are_not_declared_empty():
    value = task()
    assert value.constraints == ()
    assert value.constraints_declared is False
    assert validate_tasks([value]) == []

@pytest.mark.parametrize(("tasks", "message"), [
    ([task("x"), task("x")], "duplicate task id"),
    ([task("x", dependencies=("missing",))], "missing dependency"),
    ([Task(id="x")], "empty task"),
    ([Task(id="x", payload={"work": True})], "missing output contract"),
    ([task("x", constraints=(Constraint("limit"),))], "invalid constraint"),
])
def test_validation_errors_are_distinguishable(tasks, message):
    assert any(message in error for error in validate_tasks(tasks))

def test_declared_empty_constraints_are_explicit():
    """显式声明为空是合法输入：校验通过，Φ=TRUE，派发门禁 READY。"""
    tasks = [task("x", constraints_declared=True)]
    assert validate_tasks(tasks) == []

    report = evaluate(tasks[0])
    assert report.truth is Truth.TRUE
    assert report.dispatchable
    assert report.findings == ()

    gate = gate_task(report, analyze(build_dag(tasks), {"x": 1})["x"])
    assert gate.decision is Decision.READY
    assert any("显式声明没有额外硬约束" in reason for reason in gate.reasons)

def test_undeclared_constraints_stay_unknown_through_the_same_gate():
    """字段缺省与显式为空不同：校验同样通过，但 Φ=UNKNOWN、门禁 DEFERRED。"""
    tasks = [task("x")]  # constraints_declared=False
    assert validate_tasks(tasks) == []

    report = evaluate(tasks[0])
    assert report.truth is Truth.UNKNOWN
    assert not report.dispatchable

    gate = gate_task(report, analyze(build_dag(tasks), {"x": 1})["x"])
    assert gate.decision is Decision.DEFERRED

def test_cycle_is_invalid():
    graph = DAG((task("a", dependencies=("b",)), task("b", dependencies=("a",))))
    assert validate_graph(graph) == ["cycle detected"]

FIXED_GRAPHS = ["01_linear.json", "02_diamond.json", "03_unknown.json", "04_deadline_miss.json", "05_cycle.json"]

def test_all_fixed_graphs_load(graph_fixture_dir):
    paths = sorted(graph_fixture_dir.glob("*.json"))
    assert [path.name for path in paths] == FIXED_GRAPHS
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data["tasks"], list)

@pytest.mark.parametrize("name", FIXED_GRAPHS[:4])
def test_acyclic_fixed_graphs_are_valid(load_graph, name):
    assert validate_tasks(load_graph(name).tasks) == []

def test_cycle_fixture_is_invalid(load_graph):
    assert validate_tasks(load_graph("05_cycle.json").tasks) == ["cycle detected"]

def test_unknown_fixture_omits_constraints_and_lacks_evidence(load_graph):
    graph = load_graph("03_unknown.json")
    by_id = {task.id: task for task in graph.tasks}
    assert by_id["parse"].constraints_declared is False
    assert by_id["store"].constraints_declared is True
    assert "store" not in graph.checks

def test_timed_fixtures_cover_every_task(load_graph):
    for name in ("02_diamond.json", "03_unknown.json", "04_deadline_miss.json"):
        graph = load_graph(name)
        ids = {task.id for task in graph.tasks}
        assert set(graph.durations) == ids
        assert set(graph.deadlines) <= ids
