"""2026 National-I mathematics P15: FBS real-Lean acceptance experiment.

A scripted Root plan creates two dependent subproblems. FakeClient supplies
fixed worker proof responses. The real Lean 4 kernel validates both worker
outputs through the existing FBS Executor and its pluggable acceptance gate.

LIMIT: This is not autonomous model reasoning and not a full formal encoding of
Euclidean 3D geometry; see experiments/gaokao_2026/README.md.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from feasibility_scheduler.acceptance import AcceptanceReport
from feasibility_scheduler.events import EventLog
from feasibility_scheduler.executor import Executor
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.lean_acceptance import make_lean_acceptor
from feasibility_scheduler.models import Task
from feasibility_scheduler.store import TaskStore

# From C=(0,0,0), A=(a,0,0), B=(0,a,0), C1=(0,0,2):
# D=(a/2,a/2,0), E=(a/2,0,1).
# Theorem 1 checks the x displacement of DE is zero (parallel to x=0).
GOAL_PARALLEL = "∀ a : ℝ, (a / 2) - (a / 2) = 0"
# Angle(DE, y=0)=45 degrees gives
# (a/2)^2 / ((a/2)^2 + 1) = 1/2.
# Theorem 2 derives the distance a/2=1 from the squared-sine relation.
GOAL_DISTANCE = (
    "∀ a : ℝ, 0 < a → "
    "((a / 2)^2 / ((a / 2)^2 + 1) = (1 / 2 : ℝ)) → a / 2 = 1"
)

# These answers are intentionally SCRIPTED. They test orchestrator + Lean,
# not whether a model can discover the proof.
PROOF_PARALLEL = "by intro a; ring"
PROOF_DISTANCE = (
    "by intro a ha h; field_simp at h; "
    "nlinarith [sq_nonneg (a / 2 - 1)]"
)


def main() -> int:
    tasks = [
        Task(
            id="gaokao15_parallel",
            constraints_declared=True,
            output_contract={"type": "object"},
            payload={"prompt_text": "Prove line DE is parallel to plane BCC1B1"},
        ),
        Task(
            id="gaokao15_distance",
            dependencies=("gaokao15_parallel",),
            constraints_declared=True,
            output_contract={"type": "object"},
            payload={"prompt_text": "Derive line-to-plane distance from 45-degree angle"},
        ),
    ]
    acceptors = {
        tasks[0].id: make_lean_acceptor(
            goal_statement=GOAL_PARALLEL, lean_import="Mathlib"
        ),
        tasks[1].id: make_lean_acceptor(
            goal_statement=GOAL_DISTANCE, lean_import="Mathlib"
        ),
    }

    def lean_accept(task: Task, response) -> AcceptanceReport:
        return acceptors[task.id](task, response)

    with tempfile.TemporaryDirectory(prefix="fbs-gaokao-") as tmp:
        root = Path(tmp)
        store = TaskStore(root / "state.sqlite3")
        log = EventLog(root / "trace.jsonl", run_id="gaokao-2026-p15")
        try:
            client = FakeClient([
                Step.success(json.dumps({"proof": PROOF_PARALLEL})),
                Step.success(json.dumps({"proof": PROOF_DISTANCE})),
            ])
            outcome = Executor(
                tasks,
                store=store,
                log=log,
                client=client,
                acceptor=lean_accept,
                max_concurrency=2,
            ).run()
            result = {
                "problem": "2026 National I mathematics - P15",
                "scripted_workers": True,
                "verifier": "real Lean 4 (if installed and invoked)",
                "passed": outcome.ok,
                "states": {k: v.value for k,v in outcome.states.items()},
                "reports": {k: v.to_dict() for k,v in outcome.reports.items()},
                "verdict": outcome.verdict.to_dict(),
                "events": [
                    {"event_type": e.type, "task_id": e.task_id}
                    for e in log.events()
                ],
            }
            output_path = Path("fbs_gaokao_2026_result.json")
            output_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return outcome.exit_code
        finally:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
