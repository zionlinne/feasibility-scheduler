"""Experimental host-owned natural-language Skill acceptance adapter.

The Skill defines a rubric; a separately invoked reviewer returns structured
observations. FBS checks coverage of mandatory criteria and fails closed on
missing/unknown evidence. This does NOT make model judgments deterministic or
verify the contents of source references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .acceptance import AcceptanceReport, Acceptor
from .fake_client import ApiResponse
from .models import Task


@dataclass(frozen=True)
class ReviewSkill:
    """Host-fixed natural-language rubric and its mandatory criterion IDs."""

    name: str
    instructions: str
    criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Skill name must not be empty")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("Skill instructions must not be empty")
        if isinstance(self.criteria, str):
            raise ValueError("Skill criteria must be a sequence of IDs")
        criteria = tuple(self.criteria)
        if (not criteria or any(not isinstance(c, str) or not c.isidentifier() for c in criteria)
                or len(set(criteria)) != len(criteria)):
            raise ValueError("Skill criteria must be unique identifier strings")
        object.__setattr__(self, "criteria", criteria)


# A host-supplied reviewer callback, not a verdict copied from a Worker output.
ReviewFn = Callable[[Task, ApiResponse, ReviewSkill], Mapping[str, Any]]


def make_skill_acceptor(*, skill: ReviewSkill, reviewer: ReviewFn) -> Acceptor:
    """Return an FBS Acceptor without changing Executor or Phi.

    Reviewer response:
      {"reviewer_id": "independent-reviewer",
       "criteria": {"scope_alignment": {"status": "PASS|FAIL|UNKNOWN",
                                          "rationale": "...",
                                          "evidence": ["section or source ref"]}, ...}}

    Evidence strings are trace references; their truth is NOT verified here.
    A FAIL takes precedence over UNKNOWN; UNKNOWN alone causes RECOVERY_REQUIRED
    via the existing Executor's acceptance-error handling.
    """
    if not callable(reviewer):
        raise TypeError("reviewer must be a host-supplied callable")

    def accept(task: Task, response: ApiResponse) -> AcceptanceReport:
        if response.task_id != task.id:
            return AcceptanceReport(task.id, False, errors=("Response belongs to another task",))
        if not response.ok:
            return AcceptanceReport(task.id, False, errors=("Provider response was not successful",))
        if not isinstance(response.parsed_body, Mapping):
            return AcceptanceReport(task.id, False, errors=("Report must be a JSON object",))

        review = reviewer(task, response, skill)
        if not isinstance(review, Mapping):
            return AcceptanceReport(task.id, False, error="UNVERIFIED: malformed reviewer record")
        reviewer_id = review.get("reviewer_id")
        items = review.get("criteria")
        if (not isinstance(reviewer_id, str) or not reviewer_id.strip()
                or not isinstance(items, Mapping) or set(items) != set(skill.criteria)):
            return AcceptanceReport(task.id, False, error="UNVERIFIED: incomplete reviewer or criterion set")

        checks = [f"skill:{skill.name}:reviewer:{reviewer_id}"]
        failures: list[str] = []
        unknowns: list[str] = []
        for cid in skill.criteria:
            item = items[cid]
            if not isinstance(item, Mapping):
                return AcceptanceReport(task.id, False, error=f"UNVERIFIED: malformed criterion {cid}")
            status = item.get("status")
            rationale = item.get("rationale")
            evidence = item.get("evidence")
            if (not isinstance(status, str) or status not in {"PASS", "FAIL", "UNKNOWN"}
                    or not isinstance(rationale, str) or not rationale.strip()
                    or not isinstance(evidence, list)
                    or any(not isinstance(e, str) or not e.strip() for e in evidence)
                    or (status == "PASS" and not evidence)):
                return AcceptanceReport(task.id, False, error=f"UNVERIFIED: invalid evidence for {cid}")
            checks.append(f"skill:{skill.name}:{cid}:{status}:" + ",".join(evidence))
            if status == "FAIL":
                failures.append(f"{cid}: {rationale}")
            elif status == "UNKNOWN":
                unknowns.append(cid)

        if failures:
            return AcceptanceReport(task.id, False, tuple(checks),
                                    tuple(failures + [f"{cid}: unverified" for cid in unknowns]))
        if unknowns:
            return AcceptanceReport(task.id, False, tuple(checks),
                                    error="UNVERIFIED: " + ", ".join(unknowns))
        return AcceptanceReport(task.id, True, tuple(checks))

    return accept
