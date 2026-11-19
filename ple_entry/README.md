# PR5 / PLE adapter

`serve.py` passes the official session to `pr5_adapter.solve`. The adapter invokes
PR5's unchanged `run_legacy_doc` with NativeClient, `policy=rewrite`, modular-v1
planning and the original acceptance, concurrency and bounded repair policies.
It does not install a plan/work/verify role graph or add stage/call-count budgets.

## Task I/O

An explicit single-ZIP brief with a named file table can be imported directly.
Other layouts use one read-only model I/O import before PR5 contract generation.
That import declares requested files and explicitly specified archive membership;
it does not solve the task. Root must check the declaration against the original
brief, including omissions. No task-ID routing or benchmark answers are included.

UTF-8 deliverables use PR5's original text delivery. Binary files are generated
inside the task container through the official tools. A separately owned JSON
receipt passes through the original PR5 text interface; the adapter captures its
actual path, size and SHA-256 from disk. Optional generation script/command text
is retained in the receipt. Receipt existence is not engineering acceptance.
The adapter also accepts a substantive final-message artifact for message/MCP
tasks. These mechanisms have not yet been validated over all 29 public tasks.

## Checks

- `ple-format`: local transport/receipt syntax only.
- `ple-package-evidence`: publishes accepted text, packages only an explicitly
  declared member set, verifies exact destination, archive membership and member
  hashes, detects binary changes against the PR5 receipts, and reports actual
  initial explicit-read order and first complete candidate operation.
- `ple-root-review`: fresh conversation of the configured model; reopens actual
  outputs and uses official task tools to check engineering, public constraints,
  citations and format checks that need a domain parser. A model review is not
  licensed approval, a hidden-score certificate, or the official grader.

The generic structural auditor parses text/CSV/JSON/Python/XML, archives and
Office document containers. It reports unknown CAD/image formats, PDF rendering
and large-file format checks as **unverified**, preserving that evidence for
Root. A check of an Office container is not a rendered document/layout review.
No built-in visual inspection is supplied by the text-only `read_file` tool;
tasks needing images require suitable task tools and a compatible model route.

Early checkpoints package only real worker-written files; they create no filler
outputs and grant no acceptance. The checkpoint counter measures host tool
operations, not model turns or an official step count. Explicit read order covers
host preloading; it does not claim to trace every file read by a shell/MCP tool.

Planning guidance distinguishes intentionally unavailable grading information
from genuine missing execution prerequisites. Public targets remain unchanged.
The adapter never clears/defaults `unresolved` or changes expected predicate values.

The adapter resolves declared path aliases and deduplicates identical read paths
before checking a planning reply with the existing PR5 validator. A rejected
reply gets one chance to replan using the same request and validation error.
Genuine blockers are passed through. NativeClient validates the final reply
independently before dispatch.

## Focused validation

```
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 FEASIBILITY_RUN_INTEGRATION=0 \
  python -m pytest tests/test_pr5_ple_adapter.py tests/test_ple_interfaces.py -q
```

These tests exercise actual fixture files, archive defects and PR5's original
artifact acceptor without a live model. They are not engineering benchmark scores.
Real task results and submission artifacts must be generated with the official
`eval/run.py`; do not edit/copy another run's deliverables into a new run.
