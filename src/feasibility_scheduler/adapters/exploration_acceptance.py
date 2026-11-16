"""Opt-in qualitative acceptance for the exploration execution experiment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..acceptance import AcceptanceReport, Acceptor, contract_acceptor
from ..client import ApiResponse
from ..models import Task


def canonical_json(value: Any) -> str:
    def plain(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: plain(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(val) for val in item]
        return item

    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class ExplorationReviewRequest:
    packet_json: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.packet_json.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.packet_json)


RootReview = Callable[[ExplorationReviewRequest], Mapping[str, Any] | None]


def make_exploration_acceptor(
    *, criteria_by_task: Mapping[str, Mapping[str, str]], root_review: RootReview,
) -> Acceptor:
    """Root evaluates frozen qualitative criteria; FBS executes the verdict.

    No additional Agent, classifier, numerical score or source-count threshold.
    Root must bound callback latency. An unknown/missing/malformed review is an
    acceptance error (recovery); a substantiated revision request is rejection.
    """
    rubrics = json.loads(canonical_json(criteria_by_task))
    if not isinstance(rubrics, dict) or not rubrics:
        raise ValueError("criteria_by_task must be a nonempty mapping")
    for tid, rubric in rubrics.items():
        if not tid.strip() or not isinstance(rubric, dict) or not rubric:
            raise ValueError("every task requires a nonempty rubric")
        if any(not cid.strip() or not isinstance(text, str) or not text.strip()
               for cid, text in rubric.items()):
            raise ValueError("criterion IDs and instructions must be nonempty strings")
    if not callable(root_review):
        raise ValueError("root_review must be a host-controlled callable")

    def accept(task: Task, response: ApiResponse) -> AcceptanceReport:
        base = contract_acceptor(task, response)
        if not base.passed:
            return base
        checks = list(base.checks)
        try:
            rubric = rubrics[task.id]  # No fallback from missing semantic acceptance.
            body = response.parsed_body
            if not isinstance(body.get("report"), str) or not body["report"].strip():
                raise ValueError("a nonempty report is required")
            request = ExplorationReviewRequest(canonical_json({
                "schema": "fbs-exploration-v1", "task_id": task.id,
                "payload": task.payload, "dependencies": task.dependencies,
                "constraints_declared": task.constraints_declared,
                "constraints": [{"name": c.name, "value": c.value,
                                 "is_declared_empty": c.is_declared_empty}
                                for c in task.constraints],
                "output_contract": task.output_contract,
                "artifact": body, "criteria": rubric,
                "worker_request_id": response.request_id,
                "worker_idempotency_key": response.idempotency_key,
            }))
            checks.append(f"exploration_packet_sha256:{request.sha256}")
            review = root_review(request)
            if review is None:
                raise ValueError("Root review is missing")
            review = json.loads(canonical_json(review))
            checks.append("exploration_root_review:" + canonical_json(review))
            if not isinstance(review, dict) or set(review) != {
                "task_id", "packet_sha256", "criteria"
            }:
                raise ValueError("invalid Root review envelope")
            if review["task_id"] != task.id or review["packet_sha256"] != request.sha256:
                raise ValueError("Root review does not match the current task packet")
            findings = review["criteria"]
            if not isinstance(findings, dict) or set(findings) != set(rubric):
                raise ValueError("Root must assess every criterion, without extra criteria")
            revisions, unknown = [], []
            for cid, finding in findings.items():
                if not isinstance(finding, dict) or set(finding) != {"status", "reason"}:
                    raise ValueError("each criterion needs status and reason")
                status, reason = finding["status"], finding["reason"]
                if status not in ("pass", "revise", "unknown"):
                    raise ValueError("criterion status must be pass, revise or unknown")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("each judgment needs a substantive reason")
                checks.append(f"exploration_criterion:{cid}:{status}")
                if status == "revise":
                    revisions.append(f"{cid}: {reason}")
                elif status == "unknown":
                    unknown.append(f"{cid}: {reason}")
            if unknown:
                return AcceptanceReport(task.id, False, tuple(checks), tuple(revisions),
                                        error="Root review inconclusive: " + "; ".join(unknown))
            return AcceptanceReport(task.id, not revisions, tuple(checks), tuple(revisions))
        except Exception as exc:
            return AcceptanceReport(task.id, False, tuple(checks),
                                    error=f"exploration acceptance unavailable: {type(exc).__name__}: {exc}")

    return accept
