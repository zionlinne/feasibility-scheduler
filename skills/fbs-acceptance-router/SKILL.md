---
name: fbs-acceptance-router
description: Help an FBS Root choose acceptance evidence for mathematical, closed engineering, open research, or mixed tasks before dispatch. Use for planning an FBS run or revising its acceptance strategy.
---

Keep semantic planning with Root. Classify each work unit by the evidence its
claim needs; do not select a checker merely from its topic or file extension.

| Claim | Evidence and acceptance |
| --- | --- |
| Formal proposition | Freeze the intended proposition and use a proof checker if available. Check the translation separately. |
| Closed engineering behavior | Freeze executable predicates and independently test the actual delivered integration. |
| Open research / report | Freeze question, scope, source snapshots and qualitative criteria. Root applies the research review skill through `make_research_acceptor`; no additional Agent is required. |
| Mixed task | Route per work unit. Preserve executable checks for executable claims and separately review synthesis. |

Declare the selected strategy and its limitations before workers produce results.
Resolve unavailable checkers/evidence before dispatch, or report the obligation
as unmet. Never silently downgrade a failed executable check to language review.
Keep task dependencies, Φ, ownership, resource limits and final trace gates in
the program layer. Skills guide model choices; they do not enforce those rules.

Give Root the original question, frozen criteria, source snapshots and exact
artifact for acceptance. Keep the evaluation callback and assigned identity
host-controlled; never substitute a worker's self-reported pass flag for review.
After revisions, require review bound to the new packet; do not reuse an old pass.
For a final synthesis, recheck the original question and cross-claim consistency;
passing component reviews alone does not establish final-report quality.

Read `docs/RESEARCH_ACCEPTANCE.md` for the adapter protocol. The Lean adapter is
an independent experiment in PR #6; it is not bundled or required here. This
router advises Root; it does not add a topic classifier to the scheduler kernel.
