"""Execute contract checks and feed bounded counterexamples back to workers."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from feasibility_scheduler.acceptance import AcceptanceReport, run_acceptance
from feasibility_scheduler.fake_client import ApiRequest, FakeClient, Step
from feasibility_scheduler.models import Task

from .contract_checks import check_paths, check_repair_paths, evaluate_checks
from .workspace_validation import workspace_file_checks


def composition_task(tasks: list[Task], plan: dict[str, Any]) -> Task:
    tid = "__contract_composition__"
    if any(t.id == tid for t in tasks):
        raise ValueError("reserved contract composition task ID")
    return Task(id=tid, dependencies=tuple(t.id for t in tasks), constraints_declared=True,
                output_contract={"type": "object", "required": ["ok"]},
                payload={"contract_plan": plan, "contract_composition": True, "owned_files": [],
                         "repairable_files": sorted({p for task in tasks for p in task.payload.get("owned_files", ())}),
                         "reference_paths": sorted({p for task in tasks for p in task.payload.get("reference_files", {})}),
                         "prompt_text": "Evaluate the immutable root contract against all delivered artifacts.\nREAD-ONLY MATERIALS\n"
                         + json.dumps(tasks[0].payload.get("reference_files", {}), ensure_ascii=False)})


class ContractAcceptor:
    def __init__(self, base: Any, sink: Any, audit_dir: Path, *, repairable_files: set[str] | None = None,
                 external_checkers=None, workspace_validator=None) -> None:
        self.base = base
        self.sink = sink
        self.audit_dir = audit_dir
        self.repairable_files = repairable_files
        self.external_checkers = external_checkers
        self.workspace_validator = workspace_validator
        self.cached: dict[tuple[str, int], AcceptanceReport] = {}
        self.results: dict[str, list[dict[str, Any]]] = {}
        self.serial = max((int(p.stem.rsplit("-", 1)[1])
                           for p in audit_dir.glob("contract-checks-*.json")), default=0)

    def __call__(self, task: Task, response: Any) -> AcceptanceReport:
        cached = self.cached.pop((task.id, id(response)), None)
        return cached if cached is not None else self.evaluate(task, response)

    def evaluate(self, task: Task, response: Any) -> AcceptanceReport:
        plan = task.payload.get("contract_plan")
        if not plan or plan.get("version") != 2:
            return self.base(task, response)
        allowed = set(task.payload.get("contract_repair_files", ()))
        if allowed:
            body = response.parsed_body
            files = body.get("files") if isinstance(body, Mapping) else None
            if not response.ok or not isinstance(files, Mapping) or set(files) != allowed or any(not isinstance(v, str) for v in files.values()):
                return AcceptanceReport(task.id, False, errors=("repair must deliver exactly the assigned artifacts",))
        composition = task.payload.get("contract_composition")
        base_results = []
        if composition:
            # Only the bounded repair request is allowed to replace existing artifacts.
            if allowed:
                for path, content in files.items():
                    delivered = self.sink.deliver_file(path, content)
                    if delivered["status"] != "delivered":
                        return AcceptanceReport(task.id, False, errors=(f"repair delivery failed: {path}",))
            ids = None  # Recheck all local obligations after any composition repair.
            owned = self.repairable_files if self.repairable_files is not None else set(task.payload.get("repairable_files", ()))
            base_results = (self.workspace_validator(owned) if self.workspace_validator is not None
                            else workspace_file_checks(self.sink.workspace, owned))
        else:
            # Validate/deliver only the repair subset, then recheck the full local
            # contract against both repaired and unchanged artifacts.
            delivery_task = replace(task, payload={**task.payload, "owned_files": sorted(allowed),
                                                   "outputs": sorted(allowed)}) if allowed else task
            base_report = self.base(delivery_task, response)
            if not base_report.passed or base_report.error:
                return base_report
            ids = set(task.payload["execution_contract"]["local_checks"])
        results = list(base_results)
        if all(r["status"] == "passed" for r in base_results):
            results.extend(evaluate_checks(plan, self.sink.workspace, ids=ids, external_checkers=self.external_checkers,
                                          version_paths=(self.repairable_files or set()) | set(task.payload.get("reference_paths", ()))))
        self.results[task.id] = results
        self.serial += 1
        (self.audit_dir / f"contract-checks-{self.serial}.json").write_text(
            json.dumps({"task_id": task.id, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        failed = [f"{r['id']}: {r['detail']}" for r in results if r["status"] == "failed"]
        unknown = [r["id"] for r in results if r["status"] == "unverified"]
        return AcceptanceReport(task.id, not failed and not unknown,
                                checks=tuple(r["id"] for r in results), errors=tuple(failed),
                                error=f"UNVERIFIED: {unknown}" if unknown else None)

    def repair_files(self, task: Task) -> list[str]:
        composition = task.payload.get("contract_composition")
        if composition:
            owned = self.repairable_files if self.repairable_files is not None else set(task.payload.get("repairable_files", ()))
        else:
            owned = set(task.payload.get("owned_files", ()))
        if not composition and task.id not in self.results:
            return sorted(owned)  # Delivery failed before any contract predicate ran.
        failed = {r["id"] for r in self.results.get(task.id, ()) if r["status"] == "failed"}
        base_failures = {r["path"] for r in self.results.get(task.id, ())
                         if r["status"] == "failed" and r["id"].startswith("workspace:")} & owned
        return sorted(base_failures | {p for check in task.payload["contract_plan"]["checks"]
                                      if check["id"] in failed for p in check_repair_paths(check, owned=owned)})

    def read_context(self, task: Task, repair_files: list[str]) -> dict[str, str | None]:
        failed = {r["id"] for r in self.results.get(task.id, ()) if r["status"] == "failed"}
        relevant = [c for c in task.payload["contract_plan"]["checks"]
                    if c["id"] in failed or check_paths(c).intersection(repair_files)]
        paths = {p for c in relevant for p in check_paths(c)} - set(repair_files)
        if task.payload.get("contract_composition"):
            paths.update((self.repairable_files or set(task.payload.get("repairable_files", ()))) - set(repair_files))
        contents = {}
        root = self.sink.workspace.resolve()
        for path in sorted(paths):
            target = (root / path).resolve()
            target.relative_to(root)
            contents[path] = target.read_text(encoding="utf-8") if target.is_file() else None
        return contents


class ContractExecutionMixin:
    """Keep scheduler transitions in Executor; retry delivery before its final acceptance."""

    contract_repair_limit = 1

    async def _send(self, task_id: str, key: str) -> Any:
        task = self._task_by_id[task_id]
        if task.payload.get("contract_composition"):
            # Root validation is a local deterministic action, not another model opinion.
            response = FakeClient([Step.success()]).call(ApiRequest(
                task_id=task_id, idempotency_key=key, prompt="local composition checks",
                provider="local-contract-check"))
            self._record("api_response_received", task_id, "执行本地根契约验收", details=response.to_audit_dict())
            return response
        return await super()._send(task_id, key)

    async def _accept(self, task_id: str, response: Any) -> None:
        task = self._task_by_id[task_id]
        if not isinstance(self.acceptor, ContractAcceptor) or task.payload.get("contract_plan", {}).get("version") != 2:
            return await super()._accept(task_id, response)
        report = run_acceptance(self.acceptor, task, response)
        previous = sum(e.type == "candidate_selected" and e.component == "contract_adapter"
                       and e.task_id == task_id for e in self.log.events())
        for revision in range(previous + 1, self.contract_repair_limit + 1):
            if report is None or report.passed or report.error or not response.ok:
                break
            paths = self.acceptor.repair_files(task)
            if not paths:
                break
            artifacts = {}
            for path in paths:
                target = (self.acceptor.sink.workspace / path).resolve()
                target.relative_to(self.acceptor.sink.workspace.resolve())
                artifacts[path] = target.read_text(encoding="utf-8") if target.is_file() else None
            feedback = {"task_id": task_id, "revision": revision, "errors": list(report.errors),
                        "repair_files": paths, "artifacts": artifacts,
                        "read_only_artifacts": self.acceptor.read_context(task, paths)}
            prompt = (str(task.payload.get("prompt_text", "")) + "\nIMMUTABLE CONTRACT\n"
                      + json.dumps(task.payload["contract_plan"], ensure_ascii=False)
                      + "\nCOUNTEREXAMPLE FEEDBACK\n" + json.dumps(feedback, ensure_ascii=False)
                      + '\nRepair the assigned artifacts. Keep all original requirements and checks. '
                        'Use read_only_artifacts as current integration context; never modify or return those files. '
                        'Return only {"ok":true,"files":{"assigned/path":"complete corrected content"}}.')
            # Persist counterexamples even if the following provider request fails.
            audit_id = hashlib.sha256(task_id.encode()).hexdigest()[:16]
            (self.acceptor.audit_dir / f"contract-repair-{audit_id}-{revision}.json").write_text(
                json.dumps(feedback, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log.record("candidate_selected", component="contract_adapter", task_id=task_id,
                            candidate_id=f"contract-repair:{audit_id}:{revision}",
                            decision_reason="按验收反例修订当前工作单元交付计划", details=feedback)
            request = ApiRequest(task_id=task_id, idempotency_key=f"{response.idempotency_key}:contract-repair:{revision}",
                                 prompt=prompt, model=self.client.model, provider=self.client.provider,
                                 metadata={"artifact_delivery": True, "owned_files": paths})
            response = await self.client.acall(request)
            self._record("api_response_received" if response.ok else "api_request_failed", task_id,
                         "契约反馈修复响应", details=response.to_audit_dict())
            if not response.ok:
                report = AcceptanceReport(task_id, False, error=f"UNVERIFIED: repair response {response.outcome.value}; inspect repair audit before recovery")
                break
            for path in paths:
                self.acceptor.sink.delivery.pop(path, None)
            task = replace(task, payload={**task.payload, "contract_repair_files": paths})
            self._task_by_id[task_id] = task
            report = run_acceptance(self.acceptor, task, response)
        if report is not None:
            self.acceptor.cached[(task_id, id(response))] = report
        await super()._accept(task_id, response)
