"""Local FBS adapter for an existing PLE agent loop; no external scheduler API."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import time

from feasibility_scheduler.acceptance import AcceptanceReport
from feasibility_scheduler.client import ApiResponse, Outcome, RequestStatus
from feasibility_scheduler.events import EventLog
from feasibility_scheduler.executor import Executor
from feasibility_scheduler.models import Task
from feasibility_scheduler.store import TaskStore


class InvalidPlan(ValueError):
    pass


def parse_plan(text):
    """Freeze a small typed output contract, never execute arbitrary Python."""
    text = text.strip()
    if text.startswith('```json') and text.endswith('```'):
        text = text[7:-3].strip()
    try:
        plan = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise InvalidPlan('Planner did not return JSON') from exc
    if not isinstance(plan, dict) or set(plan) != {'summary', 'deliverables', 'checks'}:
        raise InvalidPlan('Unexpected plan fields')
    if not isinstance(plan['summary'], str) or not plan['summary'].strip():
        raise InvalidPlan('Missing plan summary')
    if not isinstance(plan['deliverables'], list) or not 1 <= len(plan['deliverables']) <= 24:
        raise InvalidPlan('Expected 1 to 24 deliverables')
    paths = set()
    for item in plan['deliverables']:
        if not isinstance(item, dict) or set(item) != {'path', 'description'}:
            raise InvalidPlan('Invalid deliverable schema')
        for field in ('path', 'description'):
            if not isinstance(item[field], str) or not item[field].strip() or '\x00' in item[field]:
                raise InvalidPlan('Invalid deliverable value')
        if item['path'] in paths:
            raise InvalidPlan('Duplicate output path')
        paths.add(item['path'])
    if not isinstance(plan['checks'], list) or not 1 <= len(plan['checks']) <= 12:
        raise InvalidPlan('Expected 1 to 12 executable checks')
    for item in plan['checks']:
        if not isinstance(item, dict) or set(item) != {'command', 'timeout_sec'}:
            raise InvalidPlan('Invalid check schema')
        if not isinstance(item['command'], str) or not item['command'].strip() or '\x00' in item['command']:
            raise InvalidPlan('Invalid check command')
        if type(item['timeout_sec']) is not int or not 1 <= item['timeout_sec'] <= 300:
            raise InvalidPlan('Invalid check timeout')
    return plan


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class QuietSession:
    """Keep the official agent API; suppress task-containing sample log lines."""
    def __init__(self, session, seconds=None):
        self.session = session
        self.instruction = session.instruction
        self.workdir = session.workdir
        self.mcp_servers = session.mcp_servers
        self.final_message = ''
        self.operations = 0
        self.deadline = None if seconds is None else time.monotonic() + seconds

    def time_left(self):
        limits = [x for x in (self.session.time_left(),
                  None if self.deadline is None else self.deadline - time.monotonic()) if x is not None]
        return max(0, min(limits)) if limits else None

    async def log(self, _text):
        # The upstream sample logs tool arguments and model errors. Hidden task
        # content must not enter retained logs, so do not forward those strings.
        return None

    async def usage(self, *args, **kwargs):
        return await self.session.usage(*args, **kwargs)

    async def exec(self, *args, **kwargs):
        self.operations += 1
        left = self.time_left()
        if left is not None:
            if left < 1:
                raise TimeoutError('Stage deadline exhausted')
            kwargs['timeout_sec'] = max(1, min(kwargs.get('timeout_sec', 120), int(left)))
        return await self.session.exec(*args, **kwargs)

    async def read_file(self, *args, **kwargs):
        self.operations += 1
        return await self.session.read_file(*args, **kwargs)

    async def write_file(self, *args, **kwargs):
        self.operations += 1
        return await self.session.write_file(*args, **kwargs)

    async def http(self, *args, **kwargs):
        self.operations += 1
        return await self.session.http(*args, **kwargs)


SNAPSHOT = '''import hashlib,json,os,sys
rows=[]
for p in json.loads(sys.argv[1]):
    if not os.path.isfile(p) or os.path.islink(p):
        raise SystemExit(2)
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)
    rows.append({'path':p,'size':os.path.getsize(p),'sha256':h.hexdigest()})
print(json.dumps(rows))'''


async def snapshot(session, plan):
    paths = [d['path'] for d in plan['deliverables']]
    command = 'python3 -c ' + shlex.quote(SNAPSHOT) + ' ' + shlex.quote(json.dumps(paths))
    result = await session.exec(command, timeout_sec=120)
    if result.get('exit_code') != 0 or result.get('timed_out') or result.get('truncated'):
        raise ValueError('Deliverable snapshot failed')
    data = json.loads(result['output'])
    if not isinstance(data, list) or len(data) != len(paths):
        raise ValueError('Incomplete deliverable snapshot')
    for expected, row in zip(paths, data):
        if (not isinstance(row, dict) or row.get('path') != expected
                or type(row.get('size')) is not int or row['size'] <= 0
                or not isinstance(row.get('sha256'), str) or len(row['sha256']) != 64):
            raise ValueError('Invalid deliverable snapshot')
    return data


@dataclass
class SessionResult:
    summary: str
    ok: bool
    states: dict
    events: tuple


class FbsLoopClient:
    provider = 'ple-local-agent-loop'

    def __init__(self, session, invoke, model):
        self.session, self.invoke, self.model = session, invoke, model
        self.outputs, self.sent, self.responses, self.attested = {}, set(), {}, set()
        self.plan, self.plan_hash = None, None
        self.accepted = set()

    def call(self, request):
        raise RuntimeError('Use the asynchronous FBS executor')

    def request_status(self, key):
        return RequestStatus.SENT if key in self.sent else RequestStatus.NOT_SENT

    async def acall(self, request):
        try:
            return await self._execute(request)
        except Exception as exc:
            key = request.idempotency_key
            now = datetime.now(timezone.utc)
            outcome = Outcome.TIMEOUT if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else Outcome.INVALID_JSON
            response = ApiResponse(
                request_id=key, idempotency_key=key, task_id=request.task_id,
                provider=self.provider, model=self.model, outcome=outcome,
                status_code=0 if outcome is Outcome.TIMEOUT else 200,
                raw_body=None, parsed_body=None, usage={}, error_type=outcome.value,
                retry_after_seconds=None, is_duplicate=False, duplicate_of=None,
                attempt=1, started_at=now, finished_at=now,
                input_artifact_hash=request.input_artifact_hash)
            self.responses[key] = response
            return response

    async def _execute(self, request):
        key, stage = request.idempotency_key, request.task_id
        if key in self.responses:
            return self.responses[key]
        if key in self.sent:
            raise RuntimeError('Unresolved request must not be dispatched twice')
        expected = {'plan': set(), 'work': {'plan'}, 'verify': {'plan', 'work'}}[stage]
        if not expected <= self.accepted:
            raise RuntimeError('Upstream acceptance missing')
        self.sent.add(key)
        started = datetime.now(timezone.utc)
        left = self.session.time_left()
        if left is not None and left < 5:
            raise TimeoutError('Task deadline exhausted')
        # A fixed three-stage DAG is deliberate: this adapter adds no DAR.
        share = {'plan': .10, 'work': .72, 'verify': .90}[stage]
        seconds = 900 if left is None else max(1, left * share)
        stage_session = QuietSession(self.session, seconds)
        if stage == 'plan':
            prompt = self.session.instruction
        elif stage == 'work':
            prompt = self.session.instruction + '\n\nFrozen plan:\n' + canonical(self.plan)
        else:
            before = await snapshot(stage_session, self.plan)
            checks = []
            for check in self.plan['checks']:
                answer = await stage_session.exec(check['command'], timeout_sec=check['timeout_sec'])
                if (answer.get('exit_code') != 0 or answer.get('timed_out')
                        or answer.get('ok') is False):
                    raise ValueError('A frozen verification command failed')
                checks.append({'command': check['command'], 'result': answer})
            # Checks may modify deliverables; freeze the exact candidate AFTER
            # those checks, then make sure the reviewer leaves it unchanged.
            before = await snapshot(stage_session, self.plan)
            prompt = (self.session.instruction + '\n\nFrozen plan:\n' + canonical(self.plan)
                      + '\n\nWorker summary:\n' + self.outputs['work']
                      + '\n\nHost check results:\n' + canonical(checks))
        operations_before = stage_session.operations
        text = await asyncio.wait_for(self.invoke(stage, stage_session, prompt), timeout=seconds)
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Empty stage output')
        if stage == 'plan':
            self.plan = parse_plan(text)
            self.plan_hash = hashlib.sha256(canonical(self.plan).encode()).hexdigest()
        elif stage == 'verify':
            if text.strip() != 'PASS':
                raise ValueError('Root review did not explicitly pass')
            if stage_session.operations <= operations_before:
                raise ValueError('Reviewer did not inspect the task environment')
            after = await snapshot(stage_session, self.plan)
            if before != after:
                raise ValueError('Candidate changed during review')
            if hashlib.sha256(canonical(self.plan).encode()).hexdigest() != self.plan_hash:
                raise ValueError('Frozen plan changed')
        self.outputs[stage] = text
        self.attested.add(key)
        response = ApiResponse(
            request_id=key, idempotency_key=key, task_id=stage,
            provider=self.provider, model=self.model, outcome=Outcome.SUCCESS,
            status_code=200, raw_body=None, parsed_body={'stage_done': True}, usage={},
            error_type=None, retry_after_seconds=None, is_duplicate=False,
            duplicate_of=None, attempt=1, started_at=started,
            finished_at=datetime.now(timezone.utc), input_artifact_hash=request.input_artifact_hash)
        self.responses[key] = response
        return response

    def accept(self, task, response):
        key = response.idempotency_key
        passed = (response.ok and response.task_id == task.id and key in self.attested
                  and self.responses.get(key) is response)
        if passed:
            self.accepted.add(task.id)
        # Keep substantive task content in memory, not the FBS audit file.
        return AcceptanceReport(task.id, passed, checks=('host_stage_evidence',),
                                errors=() if passed else ('Missing host stage evidence',))


async def run_session(session, invoke, model):
    """Entry point used by the existing harness; all FBS calls stay local."""
    client = FbsLoopClient(session, invoke, model)
    contract = {'type':'object', 'required':['stage_done'],
                'properties':{'stage_done':{'type':'boolean'}}}
    tasks = [Task(stage, dependencies=deps, constraints_declared=True,
                  payload={'role':stage}, output_contract=contract)
             for stage, deps in [('plan', ()), ('work', ('plan',)), ('verify', ('work',))]]
    # Only generic state transitions are kept temporarily; all task text and
    # outputs are memory-only. The whole temporary directory is removed at exit.
    with tempfile.TemporaryDirectory(prefix='fbs-ple-') as directory:
        log = EventLog(Path(directory) / 'events.jsonl', run_id='ple-session')
        with TaskStore(Path(directory) / 'state.sqlite3') as store:
            executor = Executor(tasks, client=client, store=store, log=log,
                                acceptor=client.accept, max_concurrency=1, max_attempts=1)
            outcome = await executor.run_async()
            events = log.events()
    states = {key: value.value for key, value in outcome.states.items()}
    summary = client.outputs.get('work', 'No completed deliverables were verified.')
    if not outcome.ok:
        summary += '\nFBS validation incomplete; engineering acceptance was not established.'
    return SessionResult(summary, outcome.ok, states, events)
