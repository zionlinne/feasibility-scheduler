"""Scripted adapter checks, not model or engineering benchmark results."""
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import shlex
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'ple_entry')]
from fbs_adapter import InvalidPlan, QuietSession, parse_plan, run_session
import taskbridge


PLAN = {'summary': 'Compute and independently check the requested memo.',
        'deliverables': [{'path': '/app/result.txt', 'description': 'Required memo'}],
        'checks': [{'command': 'check-output', 'timeout_sec': 20}]}


class ScriptedEnvironment:
    instruction = 'SENSITIVE_TEST_PROMPT: write the requested memo.'
    workdir = '/app'
    mcp_servers = []
    final_message = ''

    def __init__(self, fail_check=False):
        self.files, self.logs = {}, []
        self.fail_check = fail_check
        self.limit = 1000

    def time_left(self):
        return self.limit

    async def log(self, text):
        self.logs.append(text)

    async def usage(self, *args, **kwargs):
        pass

    async def write_file(self, path, content):
        self.files[path] = content.encode() if isinstance(content, str) else content
        return len(self.files[path])

    async def read_file(self, path):
        return self.files[path]

    async def exec(self, command, timeout_sec=120):
        if command == 'check-output':
            return {'exit_code': 1 if self.fail_check else 0, 'output': 'checked'}
        args = shlex.split(command)
        assert args[:2] == ['python3', '-c']
        rows = []
        for path in json.loads(args[3]):
            if path not in self.files:
                return {'exit_code': 2, 'output': ''}
            value = self.files[path]
            rows.append({'path': path, 'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()})
        return {'exit_code': 0, 'output': json.dumps(rows)}


def scripted_roles(env, *, verdict='PASS', inspect=True, mutate=False, bad_plan=False):
    called = []

    async def invoke(stage, view, prompt):
        called.append(stage)
        await view.log('SENSITIVE_TEST_PROMPT plus tool arguments')
        if stage == 'plan':
            return '{}' if bad_plan else json.dumps(PLAN)
        if stage == 'work':
            assert 'Frozen plan' in prompt
            await view.write_file('/app/result.txt', '42 N')
            return 'Wrote the requested memo.'
        assert 'Worker summary' in prompt and 'Host check results' in prompt
        if inspect:
            assert await view.read_file('/app/result.txt') == b'42 N'
        if mutate:
            await view.write_file('/app/result.txt', 'changed')
        return verdict

    return invoke, called


def run(env, **options):
    invoke, called = scripted_roles(env, **options)
    return asyncio.run(run_session(env, invoke, 'test-model')), called


def test_real_fbs_execution_and_release_order():
    env = ScriptedEnvironment()
    result, called = run(env)
    assert result.ok and called == ['plan', 'work', 'verify']
    assert set(result.states.values()) == {'ACCEPTED'}
    seq = lambda task, kind: next(e.sequence for e in result.events if e.task_id == task and e.type == kind)
    assert seq('plan', 'acceptance_passed') < seq('work', 'api_request_started')
    assert seq('work', 'acceptance_passed') < seq('verify', 'api_request_started')


@pytest.mark.parametrize('verdict', ['', 'pass', 'PASS.', 'UNKNOWN', 'There are gaps.'])
def test_empty_or_nonexplicit_review_never_passes(verdict):
    result, _ = run(ScriptedEnvironment(), verdict=verdict)
    assert not result.ok and result.states['verify'] != 'ACCEPTED'


def test_reviewer_must_use_environment_tools():
    result, _ = run(ScriptedEnvironment(), inspect=False)
    assert not result.ok


def test_candidate_changed_during_review_is_rejected():
    result, _ = run(ScriptedEnvironment(), mutate=True)
    assert not result.ok


def test_frozen_command_failure_blocks_review_acceptance():
    result, called = run(ScriptedEnvironment(fail_check=True))
    assert not result.ok and called == ['plan', 'work']


def test_invalid_plan_does_not_release_worker():
    result, called = run(ScriptedEnvironment(), bad_plan=True)
    assert not result.ok and called == ['plan']
    assert result.states['work'] == 'PENDING'


def test_task_text_does_not_enter_audit_or_sample_logs():
    env = ScriptedEnvironment()
    result, _ = run(env)
    assert env.logs == []
    audit = '\n'.join(json.dumps(e.to_dict(), ensure_ascii=False) for e in result.events)
    assert 'SENSITIVE_TEST_PROMPT' not in audit and '42 N' not in audit


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(checks=[]),
    lambda p: p.update(deliverables=[]),
    lambda p: p['deliverables'].append(copy.deepcopy(p['deliverables'][0])),
    lambda p: p['checks'][0].update(timeout_sec=True),
    lambda p: p['checks'][0].update(timeout_sec=301),
    lambda p: p.update(extra='unexpected'),
])
def test_plan_contract_rejects_missing_or_invalid_requirements(mutation):
    plan = copy.deepcopy(PLAN)
    mutation(plan)
    with pytest.raises(InvalidPlan):
        parse_plan(json.dumps(plan))


def test_deadline_blocks_work():
    env = ScriptedEnvironment()
    env.limit = 1
    result, called = run(env)
    assert not result.ok and called == []


def test_actual_websocket_protocol_round_trip():
    """Real loopback WebSocket, official taskbridge, scripted task/model sides."""
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    async def scenario():
        env = ScriptedEnvironment()
        observed = []

        async def solve(session):
            invoke, _ = scripted_roles(env)
            result = await run_session(session, invoke, 'test-model')
            assert result.ok
            return result.summary

        async def connection(ws):
            await taskbridge._session(ws, solve)

        async with serve(connection, '127.0.0.1', 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f'ws://127.0.0.1:{port}') as ws:
                await ws.send(json.dumps({'type':'task', 'protocol':1, 'session':'test',
                    'instruction':env.instruction, 'workdir':'/app',
                    'limits':{'agent_timeout_sec':60}, 'mcp_servers':[]}))
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    observed.append(frame['type'])
                    if frame['type'] == 'done':
                        assert 'Wrote' in frame['final_message']
                        break
                    if frame['type'] == 'write_file':
                        data = base64.b64decode(frame['content_b64'])
                        env.files[frame['path']] = data
                        answer = {'ok':True,'size':len(data)}
                    elif frame['type'] == 'read_file':
                        data = env.files[frame['path']]
                        answer = {'ok':True,'size':len(data),'content_b64':base64.b64encode(data).decode()}
                    elif frame['type'] == 'exec':
                        answer = await env.exec(frame['command'])
                    else:
                        raise AssertionError(frame['type'])
                    await ws.send(json.dumps({'type':'result','id':frame['id'],**answer}))
        assert {'write_file','read_file','exec','done'} <= set(observed)

    asyncio.run(scenario())
