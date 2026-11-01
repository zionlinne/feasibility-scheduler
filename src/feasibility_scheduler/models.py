from dataclasses import dataclass, field
from typing import Any, Mapping

@dataclass(frozen=True)
class Constraint:
    name: str
    value: Any = None
    is_declared_empty: bool = False

    def __post_init__(self):
        if not self.name.strip():
            raise ValueError("constraint name must not be empty")
        if self.is_declared_empty and self.value is not None:
            raise ValueError("an empty constraint cannot have a value")

@dataclass(frozen=True)
class Task:
    id: str
    dependencies: tuple[str, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    output_contract: Mapping[str, Any] | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    constraints_declared: bool = False

@dataclass(frozen=True)
class DAG:
    tasks: tuple[Task, ...]

@dataclass(frozen=True)
class Execution:
    task_id: str
    status: str
    output: Mapping[str, Any] | None = None

@dataclass(frozen=True)
class Result:
    success: bool
    value: Any = None
    errors: tuple[str, ...] = ()
