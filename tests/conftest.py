import json
from dataclasses import dataclass
from pathlib import Path
import pytest
from feasibility_scheduler.models import Constraint, Task
from feasibility_scheduler.phi import ConstraintCheck

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "graphs"

@dataclass(frozen=True)
class GraphFixture:
    """固定图文件解析结果。省略 constraints 字段 → constraints_declared=False。"""
    tasks: list
    durations: dict
    deadlines: dict
    checks: dict

def parse_graph_fixture(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = [
        Task(
            id=item["id"],
            dependencies=tuple(item["dependencies"]),
            constraints=tuple(Constraint(c["name"], c.get("value")) for c in item.get("constraints", ())),
            constraints_declared="constraints" in item,
            output_contract={"type": "object"},
            payload={"work": True},
        )
        for item in data["tasks"]
    ]
    checks = {
        task_id: [ConstraintCheck(c["name"], c["verdict"]) for c in items]
        for task_id, items in data.get("evidence", {}).items()
    }
    return GraphFixture(tasks, data.get("durations", {}), data.get("deadlines", {}), checks)

@pytest.fixture
def graph_fixture_dir():
    return FIXTURE_DIR

@pytest.fixture
def load_graph():
    return lambda name: parse_graph_fixture(FIXTURE_DIR / name)
