# PR5 baseline and retained PLE interfaces

This branch copies the complete tracked PR5 snapshot at
`7630497cdb0b51682488ae14c31c74dce99109c7` without modifying those files.
PR5 itself is unchanged. Its functional planning, candidate selection, execution,
acceptance, native client and tests are the baseline here.

The previous PLE-specific fixed `plan -> work -> verify` DAG, role prompts,
per-stage time shares, single-attempt policy and their tests have been removed.
No replacement workflow is introduced.

## Retained interfaces

- `taskbridge.py`: unchanged official WebSocket/session/relay/MCP transport from
  PLE commit `8320f0402fdca9f42476607f72bcfd5271cd50c9`.
- `mcp_tools.py`: task-declared MCP discovery/calls and optional read-only access.
- `serve.py`: thin entrypoint. An explicitly configured async handler,
  `FBS_PLE_ENTRYPOINT=module:function`, receives the official session unchanged.
- `submission.yaml`, dependencies, container entry and WebSocket startup check.

## Time policy

This entrypoint adds no planning/worker/reviewer deadline, call limit or attempt
limit. The official session's total task limit remains authoritative. PR5 source
is unchanged; configure any PR5 runtime timeout in the eventual adapter against
the remaining official time instead of changing PR5. The local subscription
gateway remains outside this repository and is not launched by this entrypoint.

## PR5 task connection

`FBS_PLE_ENTRYPOINT=pr5_adapter:solve` invokes PR5's original `run_legacy_doc`
with its original `NativeClient`, modular-v1 contract generation,
`GraphRewriteExecutor`, delivery acceptance, composition checks and bounded
repair settings. It imports the requested text-file catalog from the task brief
as one initial functional responsibility; PR5's planner can propose complete
decompositions. It does not replace PR5's graph with role nodes.

The current adapter supports briefs declaring a ZIP output and a table of text
deliverables. Small task documents are supplied as read-only references; the
native worker uses official task tools for larger files and computations. ZIP
creation is transport packaging of the delivered text files. Other task output
layouts fail explicitly rather than silently using a different harness.

The host registers delivery syntax validation and a substantive Root model
review with environment inspection. Root review receives the original task and
actual files, can return failed/unverified, and must leave delivered file hashes
unchanged. It uses a fresh conversation of the same configured model, not an
independent professional reviewer. Its acceptance is distinct from official PLE
grading; passing this model review does not certify engineering correctness.

The local manifest declares the actual subscription model through the local
compatible gateway. The Stage 2 target must be configured separately before
formal submission in accordance with the competition instructions.

The cleanup passed six interface checks. The task connection adds three targeted
checks for brief import, delivery parsing and actual PR5 native request exchange.
Full smoke and earlier engineering experiments are not claimed as PR5 results.
