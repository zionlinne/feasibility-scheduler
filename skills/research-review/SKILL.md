---
name: research-review
description: Independently assess an open-ended research report against host-fixed obligations, traceable primary sources and methodological limitations.
---

# Research Review Skill (Independent Acceptor)

Apply this rubric **as a reviewer**, not as the author of the artifact. The host fixes
scope, criteria, source policy and reviewer identity before a Worker starts.
The reviewer receives the original task, the submitted report, and independent
source access supplied by the host (when available).

## Required dimensions

1. `scope_alignment`: Does the report answer the original question and cover
   required subquestions without replacing the objective?
2. `source_traceability`: For material factual claims, is there a traceable
   claim-to-source map, with accessible primary evidence where required?
3. `evidence_quality`: Is cited evidence sufficient for the strength of each
   conclusion, and are empirical observations distinct from inference?
4. `methods_and_reproducibility`: Are methods, scope, dates, search criteria,
   and reproducible computations sufficiently disclosed where relevant?
5. `alternatives_and_limitations`: Are plausible alternative explanations,
   conflicting evidence, gaps, and limitations treated seriously?
6. `uncertainty_calibration`: Are unknowns, provisional findings, and the
   difference between reviewed sources and unverified claims explicit?

These are *defaults*, not a universal standard. A host may freeze a smaller
or different mandatory set for an appropriate task, but must do so *before*
workers execute; do not silently remove criteria at review time.

## Decision policy

For each fixed criterion report `PASS`, `FAIL`, or `UNKNOWN`, with a concrete
rationale and evidence references (e.g. report sections, source links, test logs).
A PASS must include evidence references; saying "looks good" is insufficient.
If reliable source verification is unavailable, use UNKNOWN rather than PASS.
If one or more criteria FAIL, the report is rejected with actionable reasons.
If no criterion FAILs but any are UNKNOWN, the result is unverified and cannot
reach ACCEPTED under FBS's existing acceptor contract. Only all-PASS is accepted.

## Reviewer output format

```json
{
  "reviewer_id": "host-confirmed-independent-reviewer",
  "criteria": {
    "scope_alignment": {
      "status": "PASS",
      "rationale": "Every original research question is addressed.",
      "evidence": ["report:section-1", "report:section-4"]
    },
    "source_traceability": {
      "status": "UNKNOWN",
      "rationale": "The primary source was inaccessible during this review.",
      "evidence": ["source:doi-or-url"]
    }
  }
}
```

Include exactly every mandatory criterion required by the host's `ReviewSkill`.
The illustrative JSON above intentionally omits the other default criteria.

## Boundaries

- A reviewer Skill is an *instruction* to a host-supplied reviewer; FBS does not
  fetch sources or invoke an LLM automatically.
- The minimal FBS adapter validates the structured review **shape**, criterion
  completeness, and nonempty evidence strings. It does not independently verify
  reviewer identity, the truth of cited evidence, or reviewer independence.
  The host must enforce those guarantees.
- Never treat a model review as equivalent to Lean proof checking.
- Do not allow workers to supply the reviewer record as their own completion claim.
