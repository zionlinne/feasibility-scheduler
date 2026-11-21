"""Compact PLE planning input for the existing PR5 graph and delivery operators."""
from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
import uuid

from feasibility_scheduler.adapters.contract_checks import requirement_ledger
from feasibility_scheduler.adapters.modular_standard import STANDARD
from ple_delivery import normalize_spec


def stage_members(delivery, members):
    """Give each explicit archive member canonical ownership and a private staging path."""
    outputs = {}
    for item in members:
        if not isinstance(item, dict):
            raise ValueError('Invalid Lite archive member')
        name, kind = item.get('name'), item.get('kind')
        if not isinstance(name, str) or not name or kind not in ('text', 'binary'):
            raise ValueError('Lite archive members need a relative name and a supported kind')
        path = PurePosixPath(name)
        if (path.as_posix() != name or path.is_absolute() or path.name in ('', '.')
                or '..' in path.parts or name in outputs or '\\' in name):
            raise ValueError('Invalid or duplicate Lite archive member path')
        outputs[name] = kind
    root = PurePosixPath(delivery.spec['root'])
    public_paths = {name: str(root / 'output' / name) for name in outputs}
    archive = delivery.spec['files'][0]['path']
    spec = normalize_spec({'files': [{'path': path, 'kind': outputs[name]} for name, path in public_paths.items()],
                           'archives': [{'path': archive, 'members': public_paths}]}, str(root))
    stage = PurePosixPath('/tmp') / ('fbs-ple-members-' + uuid.uuid4().hex)
    staged = {path: str(stage / name) for name, path in public_paths.items()}
    by_path = {item['path']: item['artifact'] for item in spec['files']}
    for item in spec['files']:
        item['path'] = staged[item['path']]
        item['staged_member'] = True
    spec['archives'][0]['members'] = {name: staged[path] for name, path in public_paths.items()}
    delivery.spec = spec
    delivery.event('pr5_lite_package_members_declared', members=list(outputs))
    return {name: by_path[path] for name, path in public_paths.items()}


async def plan_delivery(worker, source, delivery):
    archive = delivery.spec['files'][0]
    can_expand = (len(delivery.spec['files']) == 1 and not delivery.spec['archives']
                  and delivery.item_config is None and archive['kind'] == 'binary'
                  and PurePosixPath(archive['path']).suffix.lower() == '.zip')
    prompt = (
        'Plan the task using only the exact deliverable IDs below. Return JSON with '
        '{"units":[{"id":"unit-id","files":["deliverable ID"],"work":"specific responsibility",'
        '"depends_on":[],"estimated_tokens":1000}],"unresolved":[]}. The units must partition the IDs '
        'exactly once. Split by functional responsibility and add dependencies only when a completed '
        'deliverable is an actual input. One unit is valid when ownership cannot be separated. '
        'Optionally give tightly related small units the same nonempty cohesion_group to let '
        'FBS merge them before dispatch. List genuine missing inputs in unresolved. '
        'The host builds the delivery checks.\n'
    )
    if can_expand:
        prompt += (
            'The single ZIP may have independently owned members. Inspect only the supplied output '
            'specification or scope of work needed to establish the full list of required members. '
            'When that list is explicit and has at least two files, add archive_members as '
            '[{"name":"relative path in ZIP","kind":"text or binary"}, ...]. Use the member names '
            'as the units’ files in place of the ZIP ID. Respect exclusions and required names. '
            'Name an individual file only when the task leaves its filename to the author. '
            'When the complete set is not established, keep the ZIP ID and omit archive_members. '

        )
    prompt += ('TASK ROOT\n' + delivery.spec['root'] + '\nDELIVERABLE IDS\n'
               + json.dumps(delivery.names) + '\nORIGINAL TASK\n' + source)
    proposal = await worker.run(prompt, planning=True, lite_planning=True, inspect_scope=can_expand)
    if 'archive_members' in proposal:
        members = proposal['archive_members']
        if not can_expand or not isinstance(members, list) or len(members) < 2:
            raise ValueError('Lite can expand only a single ZIP with an explicit set of members')
        aliases = stage_members(delivery, members)
        units = proposal.get('units')
        if not isinstance(units, list):
            raise ValueError('Lite planning must assign all archive members')
        for unit in units:
            if (not isinstance(unit, dict) or not isinstance(unit.get('files'), list)
                    or any(not isinstance(name, str) or name not in aliases for name in unit['files'])):
                raise ValueError('Lite units must use the declared archive member names')
        proposal = {**proposal, 'units': [{**unit, 'files': [aliases[name] for name in unit['files']]}
                                          for unit in units]}
    return compile_plan(proposal, source, delivery.names)


def compile_plan(proposal, source, artifacts):
    units, blockers = proposal.get('units'), proposal.get('unresolved')
    if not isinstance(blockers, list) or any(not isinstance(x, str) or not x.strip() for x in blockers):
        raise ValueError('Lite planning requires an explicit unresolved list')
    if blockers:
        raise ValueError('Lite planning blocked: ' + '; '.join(blockers))
    if not isinstance(units, list) or not 1 <= len(units) <= len(artifacts):
        raise ValueError('Lite planning must assign the existing deliverables')
    ids, assigned = set(), []
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError('Invalid Lite work unit')
        mid, files, work, deps, budget = (unit.get(k) for k in ('id', 'files', 'work', 'depends_on', 'estimated_tokens'))
        if (not isinstance(mid, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', mid)
                or mid in ids or mid == 'submission' and len(units) > 1
                or not isinstance(files, list) or not files or any(not isinstance(f, str) for f in files)
                or not isinstance(work, str) or not work.strip() or not isinstance(deps, list)
                or any(not isinstance(d, str) for d in deps) or type(budget) is not int or budget < 1):
            raise ValueError('Invalid Lite responsibility, ownership, dependency or budget')
        ids.add(mid)
        assigned.extend(files)
    if sorted(assigned) != sorted(artifacts) or any(not set(unit['depends_on']) <= ids for unit in units):
        raise ValueError('Lite units must partition all deliverables and use known dependencies')
    if len(units) == 1 and units[0]['depends_on']:
        raise ValueError('A single Lite unit cannot wait on itself')

    ledger = requirement_ledger(source)
    source_ids = [row['id'] for row in ledger]
    receipt_checks = {name: f'delivered-{i}' for i, name in enumerate(artifacts, 1)}
    checks = [{'id': cid, 'kind': 'file_exists', 'path': name, 'requirements': source_ids}
              for name, cid in receipt_checks.items()]
    checks.append({'id': 'package-evidence', 'kind': 'external', 'name': 'ple-package-evidence',
                   'read_paths': artifacts, 'repair_paths': [], 'requirements': source_ids})
    assignments = [
        {'id': 'delivery', 'category': 'delivery', 'scope': 'local', 'owners': ['submission'],
         'checks': list(receipt_checks.values()), 'requirements': source_ids},
        {'id': 'package', 'category': 'package_execution', 'scope': 'root',
         'owners': ['__contract_composition__'], 'checks': ['package-evidence'], 'requirements': source_ids},
    ]
    modules = []
    parent = {'id': 'submission', 'files': artifacts, 'depends_on': [],
              'work': 'Complete the engineering deliverables in the original task.', 'estimated_tokens': 24000}
    if len(units) == 1:
        parent.update(work=units[0]['work'], estimated_tokens=units[0]['estimated_tokens'])
        units = []
    for unit in [parent, *units]:
        module = {'id': unit['id'], 'owned_files': unit['files'], 'execution_dependencies': unit['depends_on'],
                  'interface_dependencies': [], 'precondition': 'Required sources and upstream work are available.',
                  'postcondition': unit['work'], 'verification': 'Check the assigned files against the task.',
                  'handoff': 'Return the assigned deliverables for official evaluation.', 'boundary': unit['work'],
                  'invariants': ['Use original task requirements and preserve other units’ files.'],
                  'estimated_tokens': unit['estimated_tokens'],
                  'local_checks': [receipt_checks[f] for f in unit['files']],
                  'acceptance_limitations': 'Local checks establish delivery; official evaluation checks engineering.'}
        if 'cohesion_group' in unit:
            module['cohesion_group'] = unit['cohesion_group']
        modules.append(module)
    decompositions = [{'parent': 'submission', 'children': [
        {'id': u['id'], 'owned_files': u['files'], 'postcondition': u['work']} for u in units
    ]}] if units else []
    return {'version': 2, 'planning_standard': STANDARD, 'unresolved': [], 'requirements': ledger,
            'root': {'precondition': 'Read supplied task materials.', 'postcondition': 'Deliver the requested package.',
                     'verification': 'Run deterministic delivery checks.'},
            'composition': {'method': 'Assemble declared deliverables.', 'consistency': 'Preserve assigned ownership.',
                            'verification': 'Check the assembled delivery.',
                            'counterexample_review': 'Report missing or inconsistent files before grading.'},
            'modules': modules, 'checks': checks, 'acceptance_assignments': assignments,
            'decompositions': decompositions}
