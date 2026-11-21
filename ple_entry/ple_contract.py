"""Validate an actual model reply with PR5, and return protocol feedback to it.

No defaults, predicate rewrites, hidden answers, or bypass of PR5 validation.
"""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from feasibility_scheduler.adapters.contract_plan import generate_plan, attach_plan
from feasibility_scheduler.adapters.acceptance_profile import validate_responsibilities
from feasibility_scheduler.client import Outcome


def bind_root_repairs(candidate, tasks, event):
    """Give host package checks a repair scope over the declared deliverables."""
    owned = {path for task in tasks for path in task.payload.get('owned_files', ())}
    checks = candidate.get('checks')
    if not owned or not isinstance(checks, list):
        return
    for check in checks:
        if (not isinstance(check, dict) or check.get('kind') != 'external'
                or check.get('name') not in {'ple-root-review', 'ple-package-evidence'}):
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


def shape_errors(candidate, profile=None):
    """Collect missing structural fields together, rather than one per model call."""
    errors = []
    def text_fields(value, keys, prefix):
        if not isinstance(value, dict):
            errors.append(prefix + ': object required')
            return
        for key in keys:
            if not isinstance(value.get(key), str) or not value[key].strip():
                errors.append(prefix + '.' + key + ': nonempty string required')
    text_fields(candidate.get('root'), ('precondition', 'postcondition', 'verification'), 'root')
    text_fields(candidate.get('composition'), ('method', 'consistency', 'verification', 'counterexample_review'), 'composition')
    if 'unresolved' not in candidate or not isinstance(candidate['unresolved'], list):
        errors.append('unresolved: explicit array required; preserve real blockers')
    for key in ('requirements', 'checks', 'modules'):
        if not isinstance(candidate.get(key), list) or not candidate[key]:
            errors.append(key + ': nonempty array required')
    if profile is not None and not isinstance(candidate.get('acceptance_assignments'),list):
        errors.append('acceptance_assignments: array required by host profile')
    checks = {c['id']:c for c in candidate.get('checks',[]) if isinstance(c,dict) and isinstance(c.get('id'),str)} if isinstance(candidate.get('checks'),list) else {}
    modules = candidate.get('modules')
    if isinstance(modules, list):
        for index, module in enumerate(modules):
            prefix = 'modules[' + str(index) + ']'
            text_fields(module, ('id', 'precondition', 'postcondition', 'verification', 'handoff', 'boundary'), prefix)
            if not isinstance(module, dict):
                continue
            for key in ('owned_files', 'execution_dependencies', 'interface_dependencies', 'invariants', 'local_checks'):
                if not isinstance(module.get(key), list):
                    errors.append(prefix + '.' + key + ': array required')
            if not module.get('local_checks'):
                errors.append(prefix + '.local_checks: at least one declared check required')
            local = module.get('local_checks')
            if (isinstance(local,list) and local and all(isinstance(cid,str) and cid in checks and checks[cid].get('kind') in {'file_exists','text_contains'} for cid in local)
                    and (not isinstance(module.get('acceptance_limitations'),str) or not module['acceptance_limitations'].strip())):
                errors.append(prefix + '.acceptance_limitations: nonempty string required for structural-only local evidence')
            cost = module.get('estimated_tokens')
            if isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
                errors.append(prefix + '.estimated_tokens: positive output estimate required')
    return errors


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


def preflight(candidate, tasks, source, checkers):
    errors = shape_errors(candidate, tasks[0].payload.get('acceptance_profile') if tasks else None)
    try:
        plan = generate_plan(ReplyForValidation(candidate), tasks, source)
        validate_responsibilities(plan, tasks, checkers=checkers)
        attach_plan(tasks, plan)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        errors.append(type(exc).__name__ + ': ' + str(exc))
    return list(dict.fromkeys(errors))


def missing_slots(candidate, tasks):
    errors=shape_errors(candidate,tasks[0].payload.get('acceptance_profile') if tasks else None)
    slots={}
    for message in errors:
        name=message.split(':',1)[0]
        path=[int(part) if part.isdigit() else part for part in re.findall(r'[A-Za-z_]+|\d+',name)]
        slots[json.dumps(path)]=path
    return list(slots.values())


def apply_additions(candidate, patch, slots):
    """Apply only model-provided values to host-identified missing/blank fields."""
    if not isinstance(patch,dict) or not isinstance(patch.get('additions'),list):
        raise ValueError('Protocol feedback must return additions, not a rewritten plan')
    allowed={json.dumps(path) for path in slots}
    result=json.loads(json.dumps(candidate))
    seen=set()
    for addition in patch['additions']:
        if not isinstance(addition,dict) or 'value' not in addition:
            raise ValueError('Malformed protocol addition')
        path=addition.get('path'); key=json.dumps(path)
        if key not in allowed or key in seen:
            raise ValueError('Protocol addition targets an existing or unauthorized field')
        seen.add(key)
        target=result
        for part in path[:-1]:target=target[part]
        target[path[-1]]=addition['value']
    blockers=patch.get('unresolved',[])
    if blockers:
        if not isinstance(blockers,list) or any(not isinstance(b,str) or not b.strip() for b in blockers):
            raise ValueError('Invalid blocking questions')
        result['unresolved']=list(dict.fromkeys([*candidate.get('unresolved',[]),*blockers]))
    preserve_obligations(candidate,result)
    return result


def preserve_obligations(before, after):
    """A format correction may fill fields; it cannot remove existing evidence."""
    def same(left, right):
        return json.dumps(left,sort_keys=True,separators=(',',':')) == json.dumps(right,sort_keys=True,separators=(',',':'))
    if before.get('unresolved') not in (None, []) and before.get('unresolved') != after.get('unresolved'):
        raise ValueError('Format correction changed blocking questions')
    old_checks = before.get('checks')
    new_checks = after.get('checks')
    if isinstance(old_checks, list):
        new = {c.get('id'): c for c in (new_checks or []) if isinstance(c, dict)}
        for check in old_checks:
            if not isinstance(check, dict) or not isinstance(check.get('id'), str):
                continue
            if check['id'] not in new:
                raise ValueError('Format correction removed an existing check: ' + check['id'])
            for key, value in check.items():
                if key not in new[check['id']] or not same(new[check['id']][key],value):
                    raise ValueError('Format correction changed an existing predicate: ' + check['id'] + '.' + key)
    old_modules = before.get('modules')
    if isinstance(old_modules, list):
        new = {m.get('id'): m for m in after.get('modules', []) if isinstance(m, dict)}
        for module in old_modules:
            if not isinstance(module, dict) or not isinstance(module.get('id'), str):
                continue
            if module['id'] not in new:
                raise ValueError('Format correction removed an existing module')
            for key in ('owned_files', 'execution_dependencies', 'interface_dependencies', 'invariants', 'local_checks'):
                if isinstance(module.get(key), list) and not same(module[key],new[module['id']].get(key)):
                    raise ValueError('Format correction changed module obligations: ' + module['id'] + '.' + key)


async def prepare_contract(candidate, *, worker, tasks, source, checkers, aliases, canonicalize, event):
    """One protocol-feedback correction, followed by all original validations.

    Actual nonempty blockers return unchanged. An invalid corrected reply remains
    invalid and is sent to the original PR5 gate; no validator is weakened.
    """
    bind_root_repairs(candidate, tasks, event)
    errors = preflight(candidate, tasks, source, checkers)
    event('pr5_contract_preflight', valid=not errors, errors=errors,
          response_sha256=hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest())
    if not errors or ('unresolved' in candidate and candidate['unresolved'] != []):
        return candidate
    slots=missing_slots(candidate,tasks)
    if not slots:
        return candidate  # Semantic/dependency/capability failures are not missing-field repairs.
    prompt = (
        'Fill the listed missing fields of this same PR5 contract. Return only this JSON object: '
        '{"additions":[{"path":["composition","counterexample_review"],"value":"task-grounded text"}],'
        '"unresolved":[]}. The example path is illustrative: use ONLY the actual allowed paths below. '
        'Do not return or regenerate the entire plan. Host code applies your supplied values '
        'only at the identified missing/blank fields; existing predicates, expected values, '
        'module ownership and dependency arrays cannot be edited. No defaults are invented. '
        'If a genuine prerequisite prevents completion, report it in unresolved; do not hide it. '
        'Root requires precondition/postcondition/verification. Composition requires nonempty '
        'method/consistency/verification/counterexample_review. Modules require the original '
        'modular-v1 fields, including explicit acceptance_limitations when local checks only '
        'establish file existence/text. Explain the limitation and where verification happens '
        'under the host profile. The original PR5 validator will run again.\n'
        'ALLOWED FIELD PATHS:\n'+json.dumps(slots)+'\nVALIDATION ERRORS:\n' + json.dumps(errors) + '\nORIGINAL TASK:\n' + source +
        '\nHOST PROFILE:\n' + json.dumps(tasks[0].payload.get('acceptance_profile')) +
        '\nSAME CANDIDATE TO CORRECT:\n' + json.dumps(candidate))
    event('pr5_contract_protocol_feedback')
    try:
        patch=await worker.run(prompt,planning=True,protocol_feedback=True)
        corrected=canonicalize(apply_additions(candidate,patch,slots),aliases,planning=True)
        bind_root_repairs(corrected, tasks, event)
        preserve_obligations(candidate,corrected)
    except (ValueError, TypeError, KeyError) as exc:
        event('pr5_contract_correction_rejected', detail=str(exc))
        return candidate
    errors = preflight(corrected, tasks, source, checkers)
    event('pr5_contract_preflight', valid=not errors, errors=errors,
          response_sha256=hashlib.sha256(json.dumps(corrected, sort_keys=True).encode()).hexdigest())
    return corrected
