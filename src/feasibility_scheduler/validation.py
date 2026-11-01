from collections import defaultdict
from collections.abc import Iterable
from .models import DAG, Task

def validate_tasks(tasks: Iterable[Task]) -> list[str]:
    task_list = list(tasks)
    errors = []
    ids = [task.id for task in task_list]
    errors.extend(f"duplicate task id: {x}" for x in sorted({x for x in ids if ids.count(x) > 1}))
    known_ids = set(ids)
    for task in task_list:
        if not task.id.strip(): errors.append("empty task id")
        for dependency in task.dependencies:
            if dependency not in known_ids: errors.append(f"missing dependency: {task.id} -> {dependency}")
        if not task.payload and not task.output_contract: errors.append(f"empty task: {task.id}")
        if task.output_contract is None: errors.append(f"missing output contract: {task.id}")
        # 显式声明为空（constraints_declared=True 且集合为空）是合法输入，
        # 由 Φ 判为 TRUE；只有字段缺省才判 UNKNOWN。此处不拦截，见 docs/STAGE1_DECISIONS.md。
        for constraint in task.constraints:
            if not constraint.name.strip() or (constraint.value is None and not constraint.is_declared_empty):
                errors.append(f"invalid constraint: {task.id}:{constraint.name}")
    if not errors and _has_cycle(task_list): errors.append("cycle detected")
    return errors

def validate_graph(graph: DAG) -> list[str]:
    return validate_tasks(graph.tasks)

def _has_cycle(tasks: list[Task]) -> bool:
    edges = {task.id: task.dependencies for task in tasks}
    state = defaultdict(int)
    def visit(task_id):
        if state[task_id] == 1: return True
        if state[task_id] == 2: return False
        state[task_id] = 1
        if any(visit(dep) for dep in edges[task_id]): return True
        state[task_id] = 2
        return False
    return any(visit(task_id) for task_id in edges)
