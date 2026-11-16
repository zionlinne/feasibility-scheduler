---
name: planner-acceptance-router
description: Instruct Root to classify the evidence demands of a task, select trustworthy acceptance mechanisms, and only then plan independently checkable work.
---

# Planner & Acceptance Router (Root Skill)

This is a **natural-language planning skill**. It is not an executable router and does
not change FBS scheduling, Phi, or the Executor. The host must explicitly provide this
skill to Root. Merely storing this file in the repository does not install or invoke it.

## Sequence: acceptance first, decomposition second

1. Identify what constitutes an acceptable final artifact **from the original user request**.
   Preserve the original scope; do not weaken requirements to fit available tools.
2. Classify **acceptance obligations**, not the title or subject area of the task.
   Use one or more categories:
   - `formal`: propositions with a trusted formal statement and a checkable proof
     (e.g. Lean; formalization fidelity needs separate review).
   - `closed`: bounded requirements with deterministic executable tests,
     predicates, schema checks, or a trusted external verifier.
   - `exploratory`: open-ended investigation whose quality requires source
     checking, methodological review, alternatives and uncertainty assessment.
   - `hybrid`: sub-obligations need different verification methods.
3. Select an acceptance path for **each obligation**, before worker assignment.
   Give preference to executable/independent verification whenever available.
   Use a reviewer Skill for the irreducibly judgment-based remainder. For example,
   a mathematical research report may require formal checking for a theorem,
   executable evaluation for experiments, and research review for interpretations.
4. Identify which decisions depend on Root's *trusted* task interpretation, which
   require independent source evidence, and which require human confirmation.
5. Define subtask contracts, dependencies and the intended composition check.
   Partition work only after the acceptance boundaries are explicit.
6. Emit an `acceptance_routing_plan` for the host to approve/freeze.
   A Worker must not rewrite the obligations, select a weaker reviewer, or declare
   their own output accepted.
7. If a required verifier is absent or task semantics are ambiguous, record a
   gap as `unverified`; do not silently downgrade an obligation. Human review
   or additional evidence can resolve the gap.

## Proposed output shape (illustrative; no FBS automatic parser yet)

```json
{
  "task_type": "hybrid",
  "original_request": "...",
  "obligations": [
    {
      "id": "O1",
      "criterion": "All empirical claims cite accessible primary sources",
      "type": "exploratory",
      "verifier": "research-review-skill",
      "evidence_required": ["source identifiers", "claim-to-source mapping"]
    },
    {
      "id": "O2",
      "criterion": "Reproduce the reported metric",
      "type": "closed",
      "verifier": "trusted-test-runner",
      "evidence_required": ["test outputs", "data and code versions"]
    }
  ],
  "unverified_gaps": [],
  "worker_plan": "Decompose only after the host freezes acceptance criteria"
}
```

## Non-negotiable boundaries

- Root may propose type, tools, and decomposition; **the host freezes acceptance
  obligations** before work. Root cannot certify its own findings.
- A natural-language review is a **fallible model/human judgment**, not mathematical
  verification. Review PASS must not be presented as proof of correctness.
- `UNKNOWN` or missing independent evidence is not PASS.
- Accepted upstream artifacts must be rechecked for integrity before composition.
- For a purely exploratory question, completion may mean "well-supported,
  appropriately uncertain analysis"; it does **not** mean proving an open scientific claim.
- The FBS `Phi` feasibility gate is separate from task-specific acceptance.
