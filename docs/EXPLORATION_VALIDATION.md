# Exploration execution experiment

This independent experiment asks whether an open research task can execute
through FBS with qualitative Root acceptance. Only an opt-in acceptance adapter,
local experiment entrypoint, regression tests and this record are added. The
scheduler kernel and default callers are unchanged. PR #7 stays frozen; this
branch starts from `main` and does not include its routing skill or framework.

## Acceptance boundary

Root fixes a bounded question, any supplied source snapshots, and a qualitative
rubric before dispatch. A worker delivers `{"report": "..."}`. Root applies the
natural-language procedure in the entrypoint's `ROOT_PROCEDURE`: assess relevance,
evidence, alternatives and uncertainty against the actual frozen criteria. No
additional reviewer Agent or automatic task classifier is introduced.

`make_exploration_acceptor(criteria_by_task=..., root_review=...)` composes
existing output-contract checks with a host-controlled Root callback. Its packet
binds the task, report, rubric, source material in the payload and worker request
identity by SHA-256. Root returns:

```json
{
  "task_id": "analysis",
  "packet_sha256": "copy from Root request",
  "criteria": {
    "evidence": {"status": "pass", "reason": "Explain the actual evidential support and limits."}
  }
}
```

Every frozen criterion needs a reason and `pass`, `revise` or `unknown`. All
passes produce ACCEPTED. Revisions produce rejection/failure. Unknown, missing,
malformed or stale reviews require recovery. Nonempty text or a worker's pass
flag alone cannot authorize acceptance. An unresolved scientific answer may
still be an acceptable exploratory report if it satisfies the stated rubric.
Root's full returned judgment is retained in the existing acceptance event.

The program validates bindings and assessment coverage, not semantic correctness
of reasons, source authenticity or truth of conclusions. Root must actually
read and assess evidence. A valid structured review is not a quality oracle.

## Execute with a real report author and Root

Supply a host-owned plan; it contains no model answers or expected verdicts:

```json
{
  "tasks": [{
    "id": "analysis",
    "dependencies": [],
    "constraints": [],
    "payload": {
      "question": "Which conclusions can these materials support, and what remains unresolved?",
      "sources": {"source-1": "Insert actual source text and provenance here."}
    },
    "criteria": {
      "scope": "Address the original question within the declared scope.",
      "evidence": "Separate source facts, inference and unresolved alternatives."
    }
  }]
}
```

```sh
PYTHONPATH=src python scripts/run_exploration_experiment.py plan.json run/exploration --timeout 999
```

The run directory must be new. The entrypoint uses the actual `Executor`,
SQLite store and JSONL event log. It does not use `FakeClient`, produce answers,
call a model API, or fabricate Root verdicts. While it runs:

1. Read `requests/TASK.json` after FBS dispatches. Author the report using that
   request, then atomically write `artifacts/TASK.json` with the exact
   `idempotency_key`, `input_artifact_hash` and `artifact: {report: string}`.
2. Root reads `root_requests/TASK.json`, follows its procedure and frozen rubric,
   and atomically writes the review to `root_reviews/TASK.json` in the format above.
3. FBS applies acceptance and only then releases dependent work. Actual accepted
   upstream text is forwarded in `accepted_dependencies`; its content participates
   in the dispatch fingerprint and in the downstream Root acceptance packet.
4. Inspect `result.json`, `events.jsonl`, the original `plan.json`, artifacts and
   review packets. Exit status is nonzero when the overall run is not accepted.

Use a temporary filename then rename for external writes. Invalid envelopes
fail instead of being repaired. Declare hard constraints and actual check
evidence in `constraints` / `checks` as usual; missing facts are not auto-filled.
An explicit empty constraints list means no additional hard prerequisites.
The single-process entrypoint runs with concurrency 1; its synchronous Root
callback waits at most the configured timeout. It does not add automatic
planning, recursive decomposition, DAR, revision retries, process isolation or
cross-process recovery. Worker/Root directories are separate logical channels,
not OS access controls. The host must protect its plan/review files.

## Recorded pilot, 2026-11-16

A real file-exchange pilot used a Root-authored two-stage plan:
`analysis -> synthesis`. The question was whether the supplied FBS source could
support exploratory report execution, including a feasible path, an alternative
and unresolved limitations. Four function snapshots from `main` at `a368437`
were supplied: `Executor._release`, `Executor._accept`, `judge_run`, `evaluate`.
The stages' reports and substantive Root judgments were written during execution,
after their requests appeared; no canned answers or `FakeClient` were used.
The current assistant performed both authoring and Root acceptance. This was
not independent review, a native multi-Agent run or a blind quality experiment.

The analysis distinguished source facts from the interface-feasibility inference,
proposed Root rubric acceptance, and discussed direct Root writing as an
alternative. It warned about self-review bias and absent quality guarantees.
The synthesis used that accepted analysis to state applicability conditions and
propose defect controls plus a same-question, same-budget comparison. Both
reports deliberately retained uncertainty about research-quality improvements.

Observed results: `analysis` and `synthesis` both reached ACCEPTED; run exit 0;
SchedulerCompleted, AcceptancePass and TraceComplete were true. Before analysis
acceptance, no synthesis request existed. After acceptance, the synthesis request
contained the exact accepted analysis, and its Root packet also bound that text.
The original task's semantic standards were evaluated by Root; they were not
converted into an expected scientific answer.

The [recorded pilot bundle](experiments/exploration_pilot.json) includes the
original plan, dispatched requests, actual reports, Root packets/judgments,
20 execution events, final verdict and experiment source hashes. It records
one completed run; replaying its answers is not a fresh research experiment.

This supports one bounded execution case. It does not establish reviewer
reliability, quality gains, broad task coverage or a cost/performance advantage.
Substantive rejection and unknown-evidence blocking were exercised by scripted
tests, not by an independent live evaluator. No unspecified external gate was
counted as passed.

Validation: **595 tests passed, 1 skipped**, with live API integration disabled;
29 tests cover the new adapter/entrypoint. Tests include stale/missing reviews,
worker self-approval, unknown Φ, dependency blocking, actual file exchange,
accepted-output forwarding and artifact timeouts. Run:

```sh
FEASIBILITY_RUN_INTEGRATION=0 python -m pytest -q
```
