# Experimental research acceptance

Status: **frozen draft**. Further implementation, model wiring and live research
experiments are paused. Do not merge or treat this draft as a validated workflow.

Open research has no universal binary oracle. This optional adapter lets Root
apply a natural-language skill to a bounded report, while FBS
continues to enforce dispatch and dependency rules. An accepted report has met
the declared rubric according to that reviewer; it has not been proved true.

This experiment branches from `main`, independently of PR #6 and PRs #2–#5.
It changes no scheduler, Φ, graph, state machine, store or event implementation.
The existing `Executor(acceptor=...)` interface is sufficient. Default callers
still use the existing contract acceptor.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| Root / planner model | Understand the task, select acceptance evidence, decompose work, freeze the question and qualitative rubric. |
| Host | Own the skill evaluation callback and assigned identity, acquire source snapshots, freeze configuration and bind each review to its packet. |
| Worker / report author | Produce the actual report and evidence-linked reasoning. A worker pass flag has no authority. |
| Root during acceptance | Read `skills/fbs-research-review/SKILL.md`, apply the rubric to the exact report and evidence, return substantive assessments. No additional Agent is required. |
| Adapter | Retain output-contract checks, require complete structured assessments and exact evidence bindings, archive packet/review, translate findings to existing acceptance states. |
| FBS kernel | Enforce Φ, READY dispatch, dependency release, concurrency and trace consistency as before. |

Read `skills/fbs-acceptance-router/SKILL.md` before planning mixed-domain work.
These are repository workflow skills, not installed personal ChatGPT skills.
Routing is advice for Root; this PR does not implement a semantic classifier or
automatically change existing acceptance strategies.

## Host setup

Freeze the scope in `Task.payload` and require a JSON `report` string through
the existing output contract. Set feasibility declarations/checks as usual.
Do not mark a missing prerequisite SATISFIED merely to get a research task running.

```python
from pathlib import Path
from feasibility_scheduler.adapters.research_acceptance import make_research_acceptor

def root_skill_review(packet):
    # Connect Root's skill evaluation through the host. Send the packet JSON
    # AND packet.sha256, then return the object described in the review skill.
    # Bound callback latency; never read a worker's self_review field here.
    # This is a wiring example, not a supplied Root runtime or extra Agent.
    return root_transport.evaluate(packet.canonical_json, packet.sha256)

research_acceptor = make_research_acceptor(
    criteria={
        "scope": "Answer the original bounded question and state what remains unresolved.",
        "evidence": "Ground material claims in primary evidence; separate inference.",
        "alternatives": "Discuss contrary evidence, alternatives and practical limits.",
    },
    sources={"paper-1": frozen_primary_source_text},
    skill_text=Path("skills/fbs-research-review/SKILL.md").read_text(),
    reviewer_id="root-session",
    reviewer=root_skill_review,
    audit_dir="run/research-reviews",
)
# Executor(..., acceptor=research_acceptor).run()
```

The callback connects Root's skill evaluation without an additional reviewer
Agent or new provider dependencies. The
current acceptance interface is synchronous; callbacks must bound their own
latency and may block other work while running. It is not a new asynchronous
swarm harness. There is no automatic retry, repair or re-planning loop here.

For a mixed graph, construct acceptors with separate host rubrics and use a
host-owned `task.id -> acceptor` mapping. Fail on a missing mapping. Keep code
tests and formal proofs for those claims. Do not silently replace them with a
research review. Review the final synthesis against the original question even
if its component evidence tasks passed.

## What the program checks

1. Existing response success, task identity and output-contract checks pass.
2. The `report` is nonempty. The exact artifact, original task, worker request
   identity, rubric, source text, skill text and assigned evaluation identity are frozen
   into canonical JSON. The reviewer receives a SHA-256 fingerprint of that packet.
3. The reviewer response matches that task, fingerprint and host-assigned reviewer.
   Every frozen criterion is present exactly once. Extra fields such as a worker
   `passed` flag in a review envelope are rejected.
4. Each criterion has a `pass`, `revise` or `unknown` judgment and an explanation.
   A pass needs an exact quote present in the report and, by default, exact quotes
   from host-frozen sources. Unknown sources and invented quotes cannot pass.
5. The original packet and actual returned review are saved as content-addressed
   JSON, including rejected reviews. Audit paths, packet hash, reviewer and per-
   criterion statuses appear in `AcceptanceReport.checks`, which the existing
   event log already persists. An archive failure cannot yield acceptance.

| Review result | Existing FBS behavior |
| --- | --- |
| All criteria pass | ACCEPTED; dependent tasks may be released. |
| One or more substantive revision requests | REJECTED then FAILED; downstream remains pending. |
| Unknown evidence / missing or malformed review / callback error | RECOVERY_REQUIRED; downstream remains pending. |
| Report or rubric changes | Previous review fingerprint no longer matches; require a new review. |

Mixed `revise` and `unknown` results retain revision feedback and enter recovery
because the full review is inconclusive. There is no aggregate numeric score.
If a purely conceptual task legitimately needs no external source, the host
must explicitly set `require_sources=False`; this does not waive report evidence.

## Evidence limits and tonight's experiment

The program verifies quote presence, coverage and binding. It cannot prove that
a source is genuine, current, relevant, independent or sufficient; that a quoted
passage supports a conclusion; or that reviewer reasoning is correct. Acquiring
sources and assessing those properties remains host/reviewer work. The adapter
does not browse, sandbox sources, defend fully against prompt injection or
authenticate a person/model merely from a string. The assigned identity is an
audit label, not proof of independent review. Root performs acceptance in this
design; no author/reviewer separation gate is imposed. The host must protect the
evaluation channel and audit archive from worker edits.

Use a bounded question with frozen scope, source snapshots and a useful report
definition. When this draft is resumed, run genuine authoring and Root skill
acceptance, then test a defective
report (unsupported claim or missing counterevidence) and an evidence gap. Record
the actual outcomes and feedback, not a target pass count. Report separately:
transport/schema success, FBS gate behavior, semantic review quality and quality
of the research answer. An inconclusive research answer can be a useful report;
a missing review cannot be declared successful.

The automated tests use scripted reviews to test program behavior, including
the actual Executor dependency barrier. They are not evidence that an LLM can
reliably judge open research or that FBS improves on a matched baseline.

Run `python -m pytest tests/test_research_acceptance.py` for adapter tests, then
the repository's ordinary test suite. No API key is needed for these tests.
