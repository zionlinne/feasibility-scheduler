"""Mock model/MCP peers; exercise the actual official agent loop and adapter."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_ple_adapter import PLAN, ScriptedEnvironment
from fbs_adapter import SNAPSHOT
from mcp_tools import TaskMcpTools


class ScriptedMcpClient:
    calls = []

    def __init__(self, session, url):
        self.url = url

    async def initialize(self):
        return {}

    async def list_tools(self):
        return [{'name':'get_doc','annotations':{'readOnlyHint':True}},
                {'name':'edit_doc','annotations':{'readOnlyHint':False}}]

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        return {'content':[{'type':'text','text':'scripted MCP result'}]}


def mcp_env():
    env = ScriptedEnvironment()
    env.mcp_servers = [{'name':'docs','url':'http://127.0.0.1:8000/mcp'}]
    return env


def test_mcp_readonly_review_can_read_but_not_edit():
    async def scenario():
        client = TaskMcpTools(mcp_env(), readonly=True, client_factory=ScriptedMcpClient)
        make = lambda name: {'name':'task_mcp_call','arguments':json.dumps({'server':'docs','name':name,'arguments':{}})}
        assert 'scripted MCP result' in await client.run(make('get_doc'))
        assert 'not permitted' in await client.run(make('edit_doc'))
    asyncio.run(scenario())


def test_mcp_worker_can_call_task_tools():
    async def scenario():
        client = TaskMcpTools(mcp_env(), client_factory=ScriptedMcpClient)
        out = await client.run({'name':'task_mcp_call','arguments':json.dumps({'server':'docs','name':'edit_doc','arguments':{}})})
        assert 'scripted MCP result' in out
    asyncio.run(scenario())


def test_mcp_outside_server_rejected_before_client_initialization():
    def forbidden(*args, **kwargs):
        raise AssertionError('Must not contact an undeclared server')
    client = TaskMcpTools(mcp_env(), client_factory=forbidden)
    out = asyncio.run(client.run({'name':'task_mcp_tools','arguments':json.dumps({'server':'https://outside.example','query':''})}))
    assert 'not permitted' in out


def test_snapshot_program_hashes_real_bytes_and_rejects_symlinks(tmp_path):
    import hashlib
    output = tmp_path / 'result.txt'
    output.write_bytes(b'42 N')
    result = subprocess.run([sys.executable,'-c',SNAPSHOT,json.dumps([str(output)])],capture_output=True,text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)[0]['sha256'] == hashlib.sha256(b'42 N').hexdigest()
    output.unlink()
    result = subprocess.run([sys.executable,'-c',SNAPSHOT,json.dumps([str(output)])],capture_output=True,text=True)
    assert result.returncode == 2


def test_official_agent_loop_runs_through_actual_serve_entry(monkeypatch):
    monkeypatch.setenv('MODEL_NAME','gemini-3.8-flash')
    monkeypatch.setenv('OPENAI_API_KEY','unit-test-not-a-real-key')
    monkeypatch.setenv('OPENAI_BASE_URL','https://unit-test.invalid/v1')
    import worker_loop
    import serve

    class Message:
        def __init__(self, text=None, name=None, args=None):
            self.content = text
            self.tool_calls = None if name is None else [True]
            self.name, self.args = name, args

        def model_dump(self, **kwargs):
            value = {'role':'assistant'}
            if self.content is not None:
                value['content'] = self.content
            if self.name:
                value['tool_calls'] = [{'id':'test-call','type':'function',
                    'function':{'name':self.name,'arguments':json.dumps(self.args)}}]
            return value

    replies = iter([
        Message(json.dumps(PLAN)),
        Message(name='write_file',args={'path':'/app/result.txt','content':'42 N'}),
        Message('Wrote the requested memo.'),
        Message(name='read_file',args={'path':'/app/result.txt'}),
        Message('PASS'),
    ])
    calls = []

    async def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=next(replies),finish_reason='stop')])

    monkeypatch.setattr(worker_loop.client.chat.completions,'create',create)
    env = ScriptedEnvironment()
    summary = asyncio.run(serve.solve(env))
    assert summary == 'Wrote the requested memo.' and len(calls) == 5
    assert env.files['/app/result.txt'] == b'42 N'
    assert env.logs == []
    assert calls[0]['model'] == 'gemini-3.8-flash'
    assert 'Return ONLY JSON' in calls[0]['messages'][0]['content']
    assert 'Host check results' in calls[-1]['messages'][1]['content']
