# Functional planning without changing scheduler formulas

## Scope

The real/native adapter requests `modular-v1` for newly generated plans. The host
binds that field after generation and validates it before dispatch. Supplied older
plans without the field retain legacy candidate behavior; their execution prompts
and module responsibility projection are still corrected. This is not an automatic
semantic migration of old contracts, nor a fixed decomposition of the fulfillment
example. Artifact ownership remains host-bounded: proposals partition existing
outputs, and cannot silently add files or edit provided materials.

## Module declarations

In addition to the existing precondition, postcondition, handoff, verification and
dependency fields, each active and proposed module in `modules` declares:

- `owned_files`: exactly the artifacts assigned to this responsibility.
- `boundary`: why these outputs constitute one functional responsibility.
- `invariants`: explicit strings describing rules maintained together; `[]` is allowed.
- `local_checks`: nonempty IDs of existing executable predicates over its outputs
  and registered reference materials. Whole-workspace external suites stay at root.
- `estimated_tokens`: positive output-work estimate for this module, not a hard
  capacity limit and not a parent estimate divided by the number of files.
- `acceptance_limitations`: required when all declared local checks are existence
  or text-presence checks. It explains what those checks do not establish.
- `cohesion_group`: optional. Only explicitly matching groups may be considered
  for automatic merging, subject to the existing eligibility and scoring rules.

For example, a responsibility may own both `inventory.mjs` and `allocation.mjs`.
The planner describes its shared stock invariant and interfaces as one unit. The
mere existence of two source files does not authorize the file-splitting heuristic.
To split it, the planner must propose a valid artifact partition with complete
child declarations, interfaces and checks. If an explicitly merged request later
exhausts confirmed HTTP 5xx retries, bounded recovery restores these functional
boundaries rather than splitting every source file independently.

These fields make structural checks executable. They do not prove that a boundary
is optimal, an invariant is correctly implemented, or a declared cost is accurate.
`local_evidence` distinguishes structural-only or delivery-only acceptance from
artifact-value predicates. Independent business acceptance is still necessary.

## Formula and estimation

The benefit/cost ratio and highest-score selection rule are retained. Existing
merge and file-split heuristics keep their algebra; a functional recovery counts
functional partitions as its candidate objects. Contract-proposal candidates expose
their existing score as benefit divided by unit cost, preserving its numeric value.
Every candidate records `estimated_benefit` and `estimated_cost` for audit.

Functional output estimates are explicit module estimates, summed when modules
merge. Older proposed children without estimates retain a conservative parent
estimate instead of receiving an artificially small divided estimate. Functional
candidate cost also includes a rough compiled-input estimate. UTF-8 bytes divided
by three is only a heuristic, not a tokenizer measurement, safety bound or model
limit. Dispatch records actual prompt byte size, including available upstream
contents; that later observation is not a learned cost model or a global optimum.

No changes are made to Phi evaluation, ES/EF/LS/LF, DAG construction, concurrency
slot allocation, state-transition rules, or event replay.

## Execution and incremental checks

Workers get one current delivery contract and one current execution contract,
compiled from the immutable source catalog. A merge preserves both sources; a
split projects them and module identities to the current ownership. The full
immutable contract remains in the audit, while requests include the original
specification, relevant checks, shared interfaces and declared peer interfaces.
This avoids sending both the old parent delivery schema and the new child schema.

Functional workers must deliver every current owned file as one `files` map.
Manifest-only, partial and out-of-scope responses cannot pass by reusing old
workspace files. Failed delivery may use the existing bounded repair mechanism;
the separate integration stage keeps its one-shot behavior.

At dispatch, existing accepted execution-dependency outputs are included read-only.
Interface-only references do not invent execution waits. Non-external cross checks
run early only when their paths fit the current outputs, declared upstream outputs
and provided materials; root-only checks remain deferred. Root acceptance rechecks
the original full predicate set even if an early check passed.

## Failure evidence

Response audits now retain scrubbed exception type, message, stage, request hash
and observed HTTP status. Failure diagnostics also persist alongside raw responses.
A local response-processing bug is not classified as a provider 5xx and is not
transport-retried. Genuine HTTP 5xx retains its actual status. Non-timeout transport
failures retain the existing bounded retry behavior but cannot trigger the
confirmed-5xx merge recovery path. TIMEOUT remains UNKNOWN/recovery-required.

## Lightweight validation

Targeted tests use scripted workers and fake HTTP openers, with no model calls.
The small Node workflow creates a two-file functional module, an entrypoint and
an integration task, then runs the actual entrypoint through the independent
verifier and collects module execution evidence. Negative cases cover invalid
boundaries, missing checks, scope contamination, incomplete deliveries, failed
early cross checks, ambiguous transport failures and local processing exceptions.

The original full fulfillment task and paid-provider reliability are not tested
by these lightweight checks.
