"""Host-selected Node black-box verifier; executable paths never come from model output."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .module_execution import coverage_environment, collect_execution


def node_verifier(script: Path, report_dir: Path, timeout_seconds: float = 300, *, required_execution_files=()):
    script = script.resolve()
    if not script.is_file():
        raise ValueError(f"acceptance script does not exist: {script}")
    serial = 0

    def check(workspace: Path):
        nonlocal serial
        serial += 1
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"official-acceptance-{serial}.json"
        while report.exists():
            serial += 1
            report = report_dir / f"official-acceptance-{serial}.json"
        try:
            coverage_dir = report_dir / f"module-coverage-{serial}"
            environment = coverage_environment(coverage_dir) if required_execution_files else None
            process = subprocess.run(
                ["node", str(script), str(workspace.resolve()), str(report.resolve())],
                capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds, env=environment,
            )
            data = json.loads(report.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
                raise ValueError("acceptance report must contain a summary object")
            summary = data["summary"]
            if (data.get("selfCheckOnly") is True or not isinstance(data.get("solution"), str)
                    or Path(data["solution"]).resolve() != workspace.resolve()
                    or any(not isinstance(summary.get(k), int) or isinstance(summary[k], bool)
                           or summary[k] < 0 for k in ("passed", "failed", "error"))):
                raise ValueError("report is not an acceptance result for this solution")
            failures = [{"name": c.get("name"), "error": c.get("error"),
                         "assertions": [a for a in c.get("assertions", []) if a.get("status") != "passed"][:5]}
                        for c in data.get("cases", []) if c.get("status") != "passed"]
            passed = (process.returncode == 0 and summary["passed"] > 0
                      and summary["failed"] == 0 and summary["error"] == 0)
            detail = {"report": str(report), "summary": summary, "exit_code": process.returncode,
                      "failures": failures[:20], "uncovered": data.get("uncovered", [])}
            if required_execution_files:
                execution = collect_execution(coverage_dir, workspace, required_execution_files)
                evidence_report = report_dir / f"module-execution-{serial}.json"
                evidence_report.write_text(json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8")
                detail["module_execution"] = execution
                detail["module_execution_report"] = str(evidence_report)
                passed = passed and execution["status"] == "passed"
            return {"status": "passed" if passed else "failed", "detail": json.dumps(detail, ensure_ascii=False)}
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            return {"status": "unverified", "detail": f"acceptance runner error: {type(exc).__name__}: {exc}"}

    return check
