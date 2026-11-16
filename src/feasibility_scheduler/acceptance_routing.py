"""Minimal host-fixed per-task acceptance routing.

Root's natural-language Planner Skill may *propose* routes, but the host
binds this mapping before execution. No routing decision is read from a Worker
response, and no implicit permissive fallback exists.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .acceptance import Acceptor, AcceptanceReport
from .fake_client import ApiResponse
from .models import Task


def make_acceptance_router(routes: Mapping[str, Acceptor]) -> Acceptor:
    """Select one trusted acceptance function for each task ID.

    Supports hybrid FBS runs: formal proof, executable tests and research
    review can coexist without changing Executor. The caller MUST freeze and
    validate task IDs and obligations; this function only dispatches.
    """
    frozen = MappingProxyType(dict(routes))
    if any(not isinstance(k, str) or not k.strip() or not callable(v)
           for k, v in frozen.items()):
        raise ValueError("Every acceptance route needs a nonempty task ID and callable")

    def accept(task: Task, response: ApiResponse) -> AcceptanceReport | None:
        selected = frozen.get(task.id)
        if selected is None:
            return AcceptanceReport(
                task.id, False,
                error=f"UNVERIFIED: no host-approved acceptor for task {task.id}",
            )
        return selected(task, response)

    return accept
