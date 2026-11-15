# Lean acceptance gate: minimal experiment

This is an optional prototype for task-specific final acceptance, NOT a new scheduler or a DAR benchmark. Feasibility checks, dispatch, dependency tracking and the existing contract acceptor remain unchanged.

## Usage

Install Lean 4 and put its executable on PATH. Fix the goal in trusted configuration rather than allowing the model to rewrite it.

~~~python
from feasibility_scheduler.lean_acceptance import make_lean_acceptor

math_acceptor = make_lean_acceptor(goal_statement="(1 + 1 : Nat) = 2")
# Existing integration point:
# Executor(tasks, client, acceptor=math_acceptor, ...).run()
~~~

The model returns a JSON proof expression:

~~~json
{"proof": "by rfl"}
~~~

The adapter writes a temporary Lean source file containing the FIXED proposition, the model proof, and a check of the theorem's axioms. It requires Lean to exit successfully and the axiom report to use no nonstandard axioms, rejecting incomplete proofs such as sorry. Missing output, errors, and timeouts fail closed.

## Tests

~~~bash
python -m pytest tests/test_lean_acceptance.py
lean --version
~~~

Mock tests cover policy and error handling; an optional smoke test invokes the real Lean binary when installed and otherwise skips. The real smoke test tries one accepted proof (by rfl) and one incomplete proof (by sorry).

## Limits

- This tests a replaceable acceptance gate, not the ability to autonomously discover mathematics, and not DAR efficiency.
- The first version accepts only a single-line proof expression and imports only Init. Advanced problems will need a pinned Lean/Mathlib project.
- The trusted Lean proposition must faithfully encode the informal mathematical task.
- SECURITY: Lean permits executable metaprogramming. This prototype does NOT sandbox untrusted proof code. Do not run such code on a sensitive host without additional isolation.
- Any benchmark should hold the goal, Lean version, model and time budget fixed across baselines and log failures, acceptance and cost.
