"""Optional skill-based semantic review; the scheduling kernel stays unchanged.

The host freezes the rubric, evidence and reviewer identity before dispatch.
The reviewer callback is trusted transport, never a field in a worker response.
Only review coverage, provenance and literal evidence bindings are deterministic;
semantic quality is a fallible reviewer judgment, not a mathematical proof.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..acceptance import AcceptanceReport, Acceptor, contract_acceptor
from ..client import ApiResponse
from ..models import Task


def _json(value: Any) -> str:
    # ApiResponse / Task may contain nested MappingProxyType and tuples.
    def plain(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: plain(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(val) for val in item]
        return item

    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


@dataclass(frozen=True)
class ReviewPacket:
    """Immutable canonical JSON passed to the host's skill evaluation callback."""

    canonical_json: str

    @property
    def sha256(self) -> str:
        return _hash(self.canonical_json)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)


Reviewer = Callable[[ReviewPacket], Mapping[str, Any] | None]


def make_research_acceptor(
    *,
    criteria: Mapping[str, str],
    sources: Mapping[str, str],
    skill_text: str,
    reviewer_id: str,
    reviewer: Reviewer,
    audit_dir: str | Path,
    require_sources: bool = True,
) -> Acceptor:
    """Compose existing output-contract checks with a host-controlled review.

    Bind a factory instance to a rubric/evidence set (route per task for mixed
    graphs). Sources are host-acquired text snapshots, not worker URL claims.
    The callback must bound its own latency and authenticate reviewer identity.
    Missing, malformed or unknown reviews produce recovery-required reports;
    substantive revision requests produce rejection. There are no review retries.
    """
    rubric = dict(criteria)
    evidence = dict(sources)
    if not rubric:
        raise ValueError("at least one criterion is required")
    for key, value in [*rubric.items(), *evidence.items()]:
        _text(key, "criterion/source id")
        _text(value, "criterion/source text")
    _text(skill_text, "skill_text")
    _text(reviewer_id, "reviewer_id")
    if not callable(reviewer):
        raise ValueError("reviewer must be a host-controlled callable")
    if type(require_sources) is not bool:
        raise ValueError("require_sources must be boolean")
    if require_sources and not evidence:
        raise ValueError("source-backed review requires host-supplied sources")
    archive = Path(audit_dir)

    def persist(kind: str, content: str) -> str:
        archive.mkdir(parents=True, exist_ok=True)
        path = archive / f"{kind}-{_hash(content)}.json"
        # Content-addressed and exclusive: never silently overwrite audit data.
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != content:
                raise ValueError("audit archive content mismatch")
        return str(path.resolve())

    def accept(task: Task, response: ApiResponse) -> AcceptanceReport:
        base = contract_acceptor(task, response)
        if not base.passed:
            return base
        checks = list(base.checks)
        try:
            body = json.loads(_json(response.parsed_body))
            report = _text(body.get("report"), "artifact.report")
            packet = ReviewPacket(_json({
                "schema": "fbs-research-review-v1",
                "task_id": task.id,
                "task": {"payload": task.payload,
                         "constraints": [{"name": c.name, "value": c.value,
                                          "is_declared_empty": c.is_declared_empty}
                                         for c in task.constraints],
                         "constraints_declared": task.constraints_declared,
                         "dependencies": task.dependencies,
                         "output_contract": task.output_contract},
                "artifact": body,
                "artifact_sha256": _hash(_json(body)),
                "response_request_id": response.request_id,
                "response_idempotency_key": response.idempotency_key,
                "criteria": rubric,
                "sources": evidence,
                "require_sources": require_sources,
                "skill_text": skill_text,
                "reviewer_id": reviewer_id,
            }))
            checks.extend((f"research_packet_sha256:{packet.sha256}",
                           f"research_packet:{persist('packet', packet.canonical_json)}",
                           f"research_reviewer:{reviewer_id}"))
            review = reviewer(packet)
            if review is None:
                raise ValueError("review is missing")
            # Archive the actual response before checking its verdict.
            review = json.loads(_json(review))
            checks.append(f"research_review:{persist('review', _json(review))}")
            if not isinstance(review, dict) or set(review) != {
                "task_id", "packet_sha256", "reviewer_id", "assessments"
            }:
                raise ValueError("invalid review envelope")
            for field, expected in (("task_id", task.id),
                                    ("packet_sha256", packet.sha256),
                                    ("reviewer_id", reviewer_id)):
                if review[field] != expected:
                    raise ValueError(f"review {field} mismatch")
            items = review["assessments"]
            if not isinstance(items, list) or len(items) != len(rubric):
                raise ValueError("review must cover every criterion exactly once")
            seen: set[str] = set()
            revisions: list[str] = []
            unknown: list[str] = []
            for item in items:
                if not isinstance(item, dict) or set(item) != {
                    "criterion_id", "status", "reason", "report_quote", "source_quotes"
                }:
                    raise ValueError("invalid assessment fields")
                cid = _text(item["criterion_id"], "criterion_id")
                if cid not in rubric or cid in seen:
                    raise ValueError("unknown or duplicate criterion")
                seen.add(cid)
                status = item["status"]
                if status not in ("pass", "revise", "unknown"):
                    raise ValueError("status must be pass, revise or unknown")
                reason = _text(item["reason"], "assessment.reason")
                quote = item["report_quote"]
                if not isinstance(quote, str) or (quote and quote not in report):
                    raise ValueError("report quote is not present in artifact.report")
                if status == "pass" and not quote.strip():
                    raise ValueError("passing assessment needs a report quote")
                source_quotes = item["source_quotes"]
                if not isinstance(source_quotes, dict):
                    raise ValueError("source_quotes must map source IDs to quotes")
                for sid, excerpt in source_quotes.items():
                    if sid not in evidence or not _text(excerpt, "source quote").strip():
                        raise ValueError("unknown source or empty source quote")
                    if excerpt not in evidence[sid]:
                        raise ValueError("source quote is not present in frozen evidence")
                if status == "pass" and require_sources and not source_quotes:
                    raise ValueError("passing assessment needs source evidence")
                checks.append(f"research_criterion:{cid}:{status}")
                if status == "revise":
                    revisions.append(f"{cid}: {reason}")
                elif status == "unknown":
                    unknown.append(f"{cid}: {reason}")
            if unknown:
                return AcceptanceReport(task.id, False, tuple(checks), tuple(revisions),
                                        error="review inconclusive: " + "; ".join(unknown))
            return AcceptanceReport(task.id, not revisions, tuple(checks), tuple(revisions))
        except Exception as exc:
            return AcceptanceReport(task.id, False, tuple(checks),
                                    error=f"research review unavailable: {type(exc).__name__}: {exc}")

    return accept
