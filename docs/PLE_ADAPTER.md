# FBS PLE interface prototype

This entry wraps Google's existing `plan-verify` tool loop with the existing
FBS executor. FBS runs locally in the harness container. There is no remote
scheduler endpoint, human-in-the-loop channel, or DAR dependency.

The first integration uses a fixed `plan -> work -> verify` DAG, concurrency 1.
The planner freezes required output paths and executable checks. The worker uses
the official model/tool loop. Before final acceptance the host runs the frozen
checks, checks that every planned output is a nonempty regular file, invokes a
fresh verifier conversation, and compares file hashes before and after review.
An empty review, missing output, failed command, or changed candidate cannot pass.

FBS's plan/work acceptance means that stage's interface contract completed;
engineering review happens in the final verify stage. The verifier is another
conversation of the same configured model, not an external professional engineer.
The planner's coverage and the engineering meaning of its checks remain model
dependent. This prototype does not establish a PLE score or safety guarantee.

## Layout

- `src/feasibility_scheduler`: existing FBS source, unchanged.
- `ple_entry/fbs_adapter.py`: local scheduler/client and acceptance adapter.
- `ple_entry/serve.py`: existing-loop integration and competition startup.
- `ple_entry/taskbridge.py`, `worker_loop.py`: official PLE bridge/tool loop.
- `ple_entry/mcp_tools.py`: task-declared MCP tool access through the official bridge.
- `submission.yaml`: provisional Gemini 3.8 Flash entry; no credentials.
- `tests/`: scripted adapter, MCP, SDK-loop and real WebSocket transport checks.

No engineering task data or `artifacts/` are included. Runtime task text and model
outputs stay in memory; FBS state/audit files use a temporary directory removed
when the session ends. Persistent telemetry contains generic state names only.
The upstream sample's log messages are suppressed because they contain tool
arguments. Final deliverable summaries still go to the official evaluator.

## Check locally

Use Python 3.12 and install `requirements.txt` plus `pytest==8.4.2`.

```sh
python -m pytest tests/test_ple_adapter.py tests/test_ple_mcp_and_loop.py -q
```

Those tests use scripted model/task peers. One test uses an actual loopback
WebSocket and the official bridge; another runs the real official agent loop
with model responses replaced by test messages. None is a real PLE engineering
trial or a paid model call.

## Official checks and first public task

In a checkout of `google-research/ple`, with Docker running, the approved dataset
access and model proxy configured, point the official tools at this entry:

```sh
uv run python -m kit check /path/to/fbs-ple-entry
uv run python eval/run.py /path/to/fbs-ple-entry --tasks frame-extension-method
```

Do not hand-create `artifacts/`. It must be exported by `eval/run.py` and match
the model declared in `submission.yaml`. Do not include credentials in git.

## Current boundary

This workstation has no Docker executable, configured model proxy/project, or
dataset token available to the task. Therefore the complete official kit checks,
model connectivity, public-task run, score, cost and submission readiness have
not been verified. A specification parse is only configuration validation.

Planner/verifier direct `write_file` is unavailable; MCP calls require an
explicit read-only annotation for these roles. Their shell access is instructed
to be read-only but is not an OS permission boundary. Hash comparison detects
changes to planned outputs during review; it does not cover every task input.
No automatic revision rounds or recursive replanning are added in this prototype.

For attribution and source revisions, see `PROVENANCE.json` and
`ple_entry/LICENSE.google`. This adapter does not add a license grant for FBS.
