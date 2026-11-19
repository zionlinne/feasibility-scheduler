"""Prepare a PLE plan for the original PR5 validator."""
from __future__ import annotations

import json
from types import SimpleNamespace

from feasibility_scheduler.adapters.acceptance_profile import validate_responsibilities
from feasibility_scheduler.adapters.contract_plan import generate_plan
from feasibility_scheduler.client import Outcome


def bind_root_repairs(candidate, tasks, event):
    """Root reviews the full delivery map. Bind its fallback to host-owned outputs."""
    owned = {path for task in tasks for path in task.payload.get('owned_files', ())}
    checks = candidate.get('checks')
    if not owned or not isinstance(checks, list):
        return
    for check in checks:
        if (not isinstance(check, dict) or check.get('kind') != 'external'
                or check.get('name') != 'ple-root-review'):
            continue
        if 'paths' in check:
            legacy = check['paths']
            if ('read_paths' in check or 'repair_paths' in check or not isinstance(legacy, list)
                    or not legacy or any(not isinstance(path, str) for path in legacy)
                    or owned.intersection(legacy)):
                continue
            check['read_paths'] = check.pop('paths')
        elif check.get('repair_paths', []) != []:
            continue  # Preserve explicit scopes and let PR5 reject malformed fields.
        check.setdefault('read_paths', sorted(owned))
        check['repair_paths'] = sorted(owned)
        event('pr5_root_repair_scope_bound', files=sorted(owned))


class ReplyForValidation:
    """Replay only this already-received reply through PR5's original validator.

    This makes no model request, runs no tools/checkers and supplies no test result.
    The actual NativeClient still receives the reply and validates it independently.
    """
    model = 'received-model-reply'
    provider = 'adapter-preflight'

    def __init__(self, candidate):
        self.raw = json.dumps(candidate)

    def call(self, request):
        return SimpleNamespace(outcome=Outcome.SUCCESS, raw_body=self.raw)


def planning_error(candidate, tasks, source, checkers):
    try:
        plan = generate_plan(ReplyForValidation(candidate), tasks, source)
        validate_responsibilities(plan, tasks, checkers=checkers)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        return f'{type(exc).__name__}: {exc}'
    return None


async def prepare_contract(prompt, *, worker, tasks, source, checkers, aliases, canonicalize, event):
    """Use one full replan if the first reply fails the existing PR5 checks."""
    for attempt in (1, 2):
        candidate = canonicalize(await worker.run(prompt, planning=True), aliases, planning=True)
        bind_root_repairs(candidate, tasks, event)
        error = planning_error(candidate, tasks, source, checkers)
        event('pr5_contract_preflight', attempt=attempt, valid=error is None, errors=[error] if error else [])
        blockers = candidate.get('unresolved')
        if (error is None or attempt == 2
                or (isinstance(blockers, list) and any(isinstance(b, str) and b.strip() for b in blockers))):
            return candidate
        event('pr5_contract_replanning')
        prompt += ('\n\nPR5 rejected the previous plan: ' + error + '\n'
                   'Return a new complete plan using the same original task, ownership boundaries, '
                   'acceptance requirements and JSON schema. Recheck the whole plan before returning it. '
                   'Report genuine missing prerequisites in unresolved.')
