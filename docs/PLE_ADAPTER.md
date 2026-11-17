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

## Current boundary

The PR5 task/material/output/check adapter for PLE is not connected yet.
Without `FBS_PLE_ENTRYPOINT`, a task raises a configuration error. There is no
fallback workflow or fabricated acceptance. The entry can listen for an interface
check but cannot yet solve a PLE task. The manifest retains its Stage 2 model
and startup configuration; this branch is not ready to submit.

Cleanup validation is limited to PR5 tracked-file identity and targeted interface
checks. No full smoke, live model call or engineering task is run. Earlier
experiments belong to the removed adapter and are not PR5 results.
